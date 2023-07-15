import os
from typing import List, Optional
import numpy as np
import h5py
import librosa
import pathlib
import pickle
import matplotlib.pyplot as plt

import torch
from pytorch_lightning.core.datamodule import LightningDataModule

from jointist.data.augmentors import Augmentor
from jointist.data.samplers import (
    SegmentSampler,
    DistributedSamplerWrapper,
    CompoundSegmentSampler,
    SamplerInstrumentsClassification,
)
from jointist.data.target_processors import TargetProcessor
from jointist.utils import int16_to_float32
from jointist.config import SAMPLE_RATE, CLASSES_NUM, BEGIN_NOTE, PLUGIN_LB_TO_IX, PLUGIN_LABELS_NUM


class DataModule(LightningDataModule):
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        midi_events_hdf5s_dir: str,
        segment_seconds: float,
        hop_seconds: float,
        frames_per_second: int,
        augmentor: Augmentor,
        programs: List[str],
        batch_size: int,
        steps_per_epoch: int,
        num_workers: int,
        distributed: bool,
        mini_data: bool,
    ):
        r"""Data module.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 10.0
            hop_seconds: float, e.g., 1.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            programs: list of str, e.g., ['0', '16', '33', '48', 'percussion']
            batch_size: int
            steps_per_epoch: int
            num_workers: int
            distributed: bool
            mini_data: bool, set True to use a small amount of data for debugging
        """
        super().__init__()

        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.segment_seconds = segment_seconds
        self.hop_seconds = hop_seconds
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch
        self.mini_data = mini_data
        self.num_workers = num_workers
        self.distributed = distributed

        self.train_dataset = Dataset(
            waveform_hdf5s_dir=waveform_hdf5s_dir,
            midi_events_hdf5s_dir=midi_events_hdf5s_dir,
            segment_seconds=segment_seconds,
            frames_per_second=frames_per_second,
            augmentor=augmentor,
            programs=programs,
        )

    def setup(self, stage: Optional[str] = None):
        r"""called on every device."""

        # SegmentSampler is used for selecting segments for training.
        # On multiple devices, each SegmentSampler samples a part of mini-batch
        # data.
        _train_sampler = SegmentSampler(
            hdf5s_dir=self.waveform_hdf5s_dir,
            split='train',
            segment_seconds=self.segment_seconds,
            hop_seconds=self.hop_seconds,
            batch_size=self.batch_size,
            steps_per_epoch=self.steps_per_epoch,
            evaluation=False,
            mini_data=self.mini_data,
        )

        if self.distributed:
            self.train_sampler = DistributedSamplerWrapper(_train_sampler)
        else:
            self.train_sampler = _train_sampler

    def train_dataloader(self):
        train_loader = torch.utils.data.DataLoader(
            dataset=self.train_dataset,
            batch_sampler=self.train_sampler,
            collate_fn=collate_fn,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return train_loader


class Dataset:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        midi_events_hdf5s_dir: str,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        programs: List[str],
    ):
        r"""Dataset takes the meta of an audio segment as input, and return
        the waveform and targets of the audio segment. Dataset is used by
        DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 10.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            programs: list of str, ['0', '16', '33', '48', 'percussion']
        """

        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.midi_events_hdf5s_dir = midi_events_hdf5s_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.programs = programs
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.classes_num = CLASSES_NUM
        self.begin_note = BEGIN_NOTE

        # random seed
        self.random_state = np.random.RandomState(1234)

        # TargetProcessor is used for processing MIDI events to targets.
        self.target_processor = TargetProcessor(
            self.segment_seconds, self.frames_per_second, self.begin_note, self.classes_num
        )

        self.tmp = 0

    def __getitem__(self, meta: [str, str, float]):
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """

        [split, hdf5_name, start_time] = meta

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, split, hdf5_name)

        data_dict = {}

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:
            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            '''
            if end_sample >= hf['waveform'].shape[0]:
                start_sample -= self.segment_samples
                end_sample -= self.segment_samples
            '''
            waveform = int16_to_float32(hf['waveform'][start_sample : end_sample])
            # (segment_samples,), e.g., (160000,)

            if len(waveform) < self.segment_samples:
                valid_length = len(waveform)
                waveform = librosa.util.fix_length(waveform, size=self.segment_samples, axis=0)

            else:
                valid_length = self.segment_samples

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform
            data_dict['valid_length'] = valid_length

        # Load segment MIDI events.
        if self.midi_events_hdf5s_dir:
            midi_events_hdf5_path = os.path.join(self.midi_events_hdf5s_dir, split, hdf5_name)

            with h5py.File(midi_events_hdf5_path, 'r') as midi_events_hf:
                for program in self.programs:

                    midi_events = [e.decode() for e in midi_events_hf[program]['midi_event'][:]]
                    # E.g., [
                    # 'program_change channel=0 program=0 time=0',
                    # 'note_on channel=0 note=67 velocity=73 time=1440',
                    # ...]

                    midi_events_time = midi_events_hf[program]['midi_event_time'][:]
                    # E.g., [  0.,   1.9149, 2.3034, ...]

                    # Process MIDI events of a segment to targets, including piano
                    # rolls, onset rolls, etc.
                    (target_dict, note_events, pedal_events) = self.target_processor.process(
                        start_time, midi_events_time, midi_events, extend_pedal=True, note_shift=0
                    )
                    # E.g., target_dict = {
                    #     frame_roll: (1001, 88),
                    #     'onset_roll': (1001, 88),
                    #     ...}

                    for target_type in target_dict.keys():

                        program_target_type = '{}_{}'.format(program, target_type)
                        # E.g., '0_frame_roll' | 'percussion_frame_roll', ...

                        data_dict[program_target_type] = target_dict[target_type]

        # DO NOT DELETE. FOR DEBUG NOW.
        # if self.tmp == 1:
        # if valid_length < self.segment_samples // 2:
        # if True:
        #     import soundfile
        #     import matplotlib.pyplot as plt
        #     from IPython import embed; embed(using=False); os._exit(0)
        #     # add(note_events, start_time)
        #     soundfile.write(file='_zz.wav', data=data_dict['waveform'], samplerate=16000)
        #     # plt.matshow(data_dict['percussion_frame_roll'].T, origin='lower', aspect='auto', cmap='jet')
        #     # plt.savefig('_zz.pdf')
        #     from IPython import embed; embed(using=False); os._exit(0)

        self.tmp += 1 

        debugging = False
        if debugging:
            plot_waveform_midi_targets(data_dict, start_time, note_events)
            exit()

        return data_dict


def add(note_events, start_time):
    import pretty_midi
    new_midi_data = pretty_midi.PrettyMIDI()
    new_track_program = pretty_midi.instrument_name_to_program('Acoustic Grand Piano')
    new_track = pretty_midi.Instrument(program=new_track_program)
    new_track.name = 'Piano'
    for note in note_events: 
        if note['onset_time'] - start_time > 0:
            new_note = pretty_midi.Note(
                pitch=note['midi_note'], 
                start=note['onset_time'] - start_time, 
                end=note['offset_time'] - start_time, 
                # velocity=note['velocity']
                velocity=100
            )
            new_track.notes.append(new_note)
    new_midi_data.instruments.append(new_track)
    new_midi_data.write('_zz.mid')



def collate_fn(list_data_dict):
    r"""Collate input and target of segments to a mini-batch.

    Args:
        list_data_dict: e.g. [
            {'waveform': (segment_samples,), 'frame_roll': (segment_frames, classes_num), ...},
            {'waveform': (segment_samples,), 'frame_roll': (segment_frames, classes_num), ...},
            ...]

    Returns:
        data_dict: e.g. {
            'waveform': (batch_size, segment_samples)
            'frame_roll': (batch_size, segment_frames, classes_num),
            ...}
    """
    np_data_dict = {}
    for key in list_data_dict[0].keys():
        np_data_dict[key] = torch.Tensor(np.array([data_dict[key] for data_dict in list_data_dict]))

    return np_data_dict


class CompoundDataModule(LightningDataModule):
    def __init__(
        self,
        list_waveform_hdf5s_dir: str,
        list_midi_events_hdf5s_dir: str,
        segment_seconds: float,
        hop_seconds: float,
        frames_per_second: int,
        augmentor: Augmentor,
        list_programs: List[List[str]],
        batch_size: int,
        steps_per_epoch: int,
        num_workers: int,
        distributed: bool,
        mini_data: bool,
    ):
        r"""Data module.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 10.0
            hop_seconds: float, e.g., 1.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            programs: list of str, e.g., ['0', '16', '33', '48', 'percussion']
            batch_size: int
            steps_per_epoch: int
            num_workers: int
            distributed: bool
            mini_data: bool, set True to use a small amount of data for debugging
        """
        super().__init__()

        self.list_waveform_hdf5s_dir = list_waveform_hdf5s_dir
        self.segment_seconds = segment_seconds
        self.hop_seconds = hop_seconds
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch
        self.mini_data = mini_data
        self.num_workers = num_workers
        self.distributed = distributed

        self.train_dataset = CompoundDataset(
            list_waveform_hdf5s_dir=list_waveform_hdf5s_dir,
            list_midi_events_hdf5s_dir=list_midi_events_hdf5s_dir,
            segment_seconds=segment_seconds,
            frames_per_second=frames_per_second,
            augmentor=augmentor,
            list_programs=list_programs,
        )

    def setup(self, stage: Optional[str] = None):
        r"""called on every device."""

        # SegmentSampler is used for selecting segments for training.
        # On multiple devices, each SegmentSampler samples a part of mini-batch
        # data.
        _train_sampler = CompoundSegmentSampler(
            list_hdf5s_dir=self.list_waveform_hdf5s_dir,
            split='train',
            segment_seconds=self.segment_seconds,
            hop_seconds=self.hop_seconds,
            batch_size=self.batch_size,
            steps_per_epoch=self.steps_per_epoch,
            evaluation=False,
            mini_data=self.mini_data,
        )

        if self.distributed:
            self.train_sampler = DistributedSamplerWrapper(_train_sampler)
        else:
            self.train_sampler = _train_sampler

    def train_dataloader(self):
        train_loader = torch.utils.data.DataLoader(
            dataset=self.train_dataset,
            batch_sampler=self.train_sampler,
            collate_fn=collate_fn,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return train_loader


def energy(x, valid_length):
    return np.sum(x ** 2) / valid_length


class CompoundDataset:
    def __init__(
        self,
        list_waveform_hdf5s_dir: List[str],
        list_midi_events_hdf5s_dir: List[str],
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        list_programs: List[List[str]],
    ):
        r"""Dataset takes the meta of an audio segment as input, and return
        the waveform and targets of the audio segment. Dataset is used by
        DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 10.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            programs: list of str, ['0', '16', '33', '48', 'percussion']
        """

        self.datasets = []

        datasets_num = len(list_waveform_hdf5s_dir)

        for n in range(datasets_num):

            dataset = Dataset(
                waveform_hdf5s_dir=list_waveform_hdf5s_dir[n],
                midi_events_hdf5s_dir=list_midi_events_hdf5s_dir[n],
                segment_seconds=segment_seconds,
                frames_per_second=frames_per_second,
                augmentor=augmentor,
                programs=list_programs[n],
            )

            self.datasets.append(dataset)

        self.tmp = 0

    def __getitem__(self, list_meta):
        # : List[[str, str, float]]
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """
        assert len(list_meta) == 5
        datasets_num = len(self.datasets)

        list_data_dict = []

        for k in range(datasets_num):
            data_dict = self.datasets[k].__getitem__(list_meta[k])
            list_data_dict.append(data_dict)

        new_data_dict = {}

        for data_dict in list_data_dict:
            for key in data_dict.keys():
                assert key not in new_data_dict.keys()
                if key not in ['waveform', 'valid_length']:
                    new_data_dict[key] = data_dict[key]

        e1 = energy(list_data_dict[0]['waveform'], list_data_dict[0]['valid_length'])
        e2 = energy(list_data_dict[3]['waveform'], list_data_dict[3]['valid_length'])
        if e1 == 0:
            ratio = 0
        else:
            ratio = (e2 / e1) ** 0.5

        if e2 == 0:
            ratio = 1

        new_data_dict['waveform'] = ratio * list_data_dict[0]['waveform']
        
        for k in [1, 2, 4]:
            new_data_dict['waveform'] += list_data_dict[k]['waveform']

        # new_data_dict['waveform'] = np.sum([data_dict['waveform'] for data_dict in list_data_dict], axis=0)

        # if self.tmp == 5:
        # #     import soundfile
        # # if True:
        # # if list_data_dict[0]['valid_length'] < list_data_dict[3]['valid_length'] // 2:
        #     import soundfile
        #     soundfile.write(file='_zz.wav', data=new_data_dict['waveform'], samplerate=16000)
        #     soundfile.write(file='_zz2.wav', data=list_data_dict[0]['waveform']*ratio, samplerate=16000)
        #     soundfile.write(file='_zz3.wav', data=list_data_dict[1]['waveform']+list_data_dict[2]['waveform']+list_data_dict[4]['waveform'], samplerate=16000)
        #     from IPython import embed; embed(using=False); os._exit(0)

        self.tmp += 1

        # if self.tmp == 120:
        debugging = False
        if debugging:
            plot_waveform_midi_targets(new_data_dict, start_time=0, note_events=None)
            exit()
        # self.tmp += 1

        return new_data_dict


class DataModuleInstrumentsClassification(LightningDataModule):
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkl_path: str,
        segment_seconds: float,
        frames_per_second: int,
        augmentor: Augmentor,
        classes_num: int,
        batch_size: int,
        steps_per_epoch: int,
        num_workers: int,
        distributed: bool,
        mini_data: bool,
    ):
        r"""Instrument classification data module.

        Args:
            waveform_hdf5s_dir: str
            notes_pkl_pth: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            classes_num: int, plugins number, e.g., 167
            batch_size: int
            steps_per_epoch: int
            num_workers: int
            distributed: bool
            mini_data: bool, set True to use a small amount of data for debugging
        """
        super().__init__()

        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkl_path = notes_pkl_path
        self.segment_seconds = segment_seconds
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch
        self.mini_data = mini_data
        self.num_workers = num_workers
        self.distributed = distributed

        self.train_dataset = DatasetInstrumentsClassification(
            waveform_hdf5s_dir=waveform_hdf5s_dir,
            segment_seconds=segment_seconds,
            frames_per_second=frames_per_second,
            augmentor=augmentor,
            classes_num=classes_num,
        )

    def setup(self, stage: Optional[str] = None):
        r"""called on every device."""

        # SegmentSampler is used for selecting segments for training.
        # On multiple devices, each SegmentSampler samples a part of mini-batch
        # data.
        _train_sampler = SamplerInstrumentsClassification(
            hdf5s_dir=self.waveform_hdf5s_dir,
            notes_pkl_path=self.notes_pkl_path,
            split='train',
            segment_seconds=self.segment_seconds,
            batch_size=self.batch_size,
            steps_per_epoch=self.steps_per_epoch,
            evaluation=False,
            mini_data=self.mini_data,
        )

        if self.distributed:
            self.train_sampler = DistributedSamplerWrapper(_train_sampler)
        else:
            self.train_sampler = _train_sampler

    def train_dataloader(self):
        train_loader = torch.utils.data.DataLoader(
            dataset=self.train_dataset,
            batch_sampler=self.train_sampler,
            collate_fn=collate_fn,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return train_loader


class DatasetInstrumentsClassification:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        classes_num: int,
    ):
        r"""Instrument classification dataset takes the meta of an audio
        segment as input, and return the waveform, onset_roll, and targets of
        the audio segment. Dataset is used by DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
        """
        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.classes_num = classes_num
        self.begin_note = BEGIN_NOTE
        self.piano_notes_num = CLASSES_NUM
        self.plugin_lb_to_ix = PLUGIN_LB_TO_IX

        # random seed
        self.random_state = np.random.RandomState(1234)

    def __getitem__(self, meta):
        r"""Get input and target of a segment for training.

        Args:
            meta: dict, e.g., {
                'split': 'train',
                'audio_name': 'Track00121',
                'plugin_name': 'nylon_guitar2',
                'start': 73.1091,
                'end': 73.1827,
                'pitch': 50,
                'velocity': 121,
            }

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, piano_notes_num)
            'target': (plugin_names_num,)
        """

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, meta['split'], '{}.h5'.format(meta['audio_name']))

        plugin_name = meta['plugin_name']
        pitch = meta['pitch']
        onset_time = meta['start']

        plugin_id = self.plugin_lb_to_ix[plugin_name]

        data_dict = {}

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:

            start_time = onset_time - self.segment_seconds / 2

            if start_time < 0:
                start_time = 0

            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            if end_sample >= hf['waveform'].shape[0]:
                start_sample -= self.segment_samples
                end_sample -= self.segment_samples

            waveform = int16_to_float32(hf['waveform'][start_sample:end_sample])
            # (segment_samples,), e.g., (160000,)

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform

        # Onset roll as input.
        data_dict['onset_roll'] = get_single_note_onset_roll(
            segment_seconds=self.segment_seconds,
            frames_per_second=self.frames_per_second,
            piano_notes_num=self.piano_notes_num,
            piano_note=pitch - BEGIN_NOTE,
        )

        # target
        target = np.zeros(self.classes_num)  # (plugin_names_num,)
        target[plugin_id] = 1
        data_dict['target'] = target

        return data_dict


def get_single_note_onset_roll(segment_seconds, frames_per_second, piano_notes_num, piano_note):
    r"""Convert a note into an onset roll.

    Args:
        segment_seconds: float, e.g., 2.0.
        frames_per_second: int
        piano_notes_num: int, e.g., 88
        piano_note: int

    Returns:
        onset_roll: (frames_num, piano_notes_num)
    """

    frames_num = int(segment_seconds * frames_per_second + 1)
    center_idx = frames_num // 2
    onset_roll = np.zeros((frames_num, piano_notes_num))

    J = 5

    for i in range(J):
        onset_roll[center_idx - i, piano_note] = 1.0 - (1.0 / J) * i
        onset_roll[center_idx + i, piano_note] = 1.0 - (1.0 / J) * i

    return onset_roll


class DataModuleInstrumentsCluster(LightningDataModule):
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir: str,
        segment_seconds: float,
        hop_seconds: float,
        frames_per_second: int,
        augmentor: Augmentor,
        classes_num,
        batch_size: int,
        steps_per_epoch: int,
        num_workers: int,
        distributed: bool,
        mini_data: bool,
        max_instruments_num,
    ):
        r"""Instrument classification data module.

        Args:
            waveform_hdf5s_dir: str
            notes_pkl_pth: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            classes_num: int, plugins number, e.g., 167
            batch_size: int
            steps_per_epoch: int
            num_workers: int
            distributed: bool
            mini_data: bool, set True to use a small amount of data for debugging
        """
        super().__init__()

        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.hop_seconds = hop_seconds
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch
        self.mini_data = mini_data
        self.num_workers = num_workers
        self.distributed = distributed
        self.max_instruments_num = max_instruments_num

        self.train_dataset = DatasetInstrumentsCluster(
            waveform_hdf5s_dir=waveform_hdf5s_dir,
            notes_pkls_dir=notes_pkls_dir,
            segment_seconds=segment_seconds,
            frames_per_second=frames_per_second,
            augmentor=augmentor,
            max_instruments_num=max_instruments_num,
        )

    def setup(self, stage: Optional[str] = None):
        r"""called on every device."""

        # SegmentSampler is used for selecting segments for training.
        # On multiple devices, each SegmentSampler samples a part of mini-batch
        # data.
        _train_sampler = SegmentSampler(
            hdf5s_dir=self.waveform_hdf5s_dir,
            split='train',
            segment_seconds=self.segment_seconds,
            hop_seconds=self.hop_seconds,
            batch_size=self.batch_size,
            steps_per_epoch=self.steps_per_epoch,
            evaluation=False,
            mini_data=self.mini_data,
        )

        if self.distributed:
            self.train_sampler = DistributedSamplerWrapper(_train_sampler)
        else:
            self.train_sampler = _train_sampler

    def train_dataloader(self):
        train_loader = torch.utils.data.DataLoader(
            dataset=self.train_dataset,
            batch_sampler=self.train_sampler,
            collate_fn=collate_fn,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return train_loader

'''
class DatasetInstrumentsCluster:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        max_instruments_num,
    ):
        r"""Instrument classification dataset takes the meta of an audio
        segment as input, and return the waveform, onset_roll, and targets of
        the audio segment. Dataset is used by DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
        """
        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.begin_note = BEGIN_NOTE
        self.piano_notes_num = CLASSES_NUM
        self.plugin_lb_to_ix = PLUGIN_LB_TO_IX
        self.max_instruments_num = max_instruments_num

        # random seed
        self.random_state = np.random.RandomState(1234)

    def __getitem__(self, meta: [str, str, float]):
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """

        [split, hdf5_name, start_time] = meta

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, split, hdf5_name)

        data_dict = {}
        # print(hdf5_name, start_time, split)

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:
            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            waveform = int16_to_float32(hf['waveform'][start_sample : end_sample])
            # (segment_samples,), e.g., (160000,)

            if len(waveform) < self.segment_samples:
                valid_length = len(waveform)
                waveform = librosa.util.fix_length(waveform, size=self.segment_samples, axis=0)

            else:
                valid_length = self.segment_samples

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform
            data_dict['valid_length'] = valid_length

        pkl_path = os.path.join(self.notes_pkls_dir, '{}.pkl'.format(pathlib.Path(hdf5_name).stem))
        events_dict = pickle.load(open(pkl_path, 'rb'))

        frames_num = int(self.frames_per_second * self.segment_seconds) + 1

        mixture_onset_roll = np.zeros((frames_num, self.piano_notes_num))
        mixture_frame_roll = np.zeros((frames_num, self.piano_notes_num))
        sep_onset_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))
        sep_frame_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))

        i = 0
        for key in events_dict.keys():
            # ['S00', 'S01', ...]

            # from IPython import embed; embed(using=False); os._exit(0)
            # print(events_dict[key]['program_num'])

            sep_onset_roll = np.zeros((frames_num, self.piano_notes_num))
            sep_frame_roll = np.zeros((frames_num, self.piano_notes_num))

            for note_event in events_dict[key]['note_event']:
                if (note_event['start'] > start_time and note_event['start'] < start_time + self.segment_seconds) or \
                (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):

                    bgn_frame = int((note_event['start'] - start_time) * self.frames_per_second)
                    bgn_frame = max(0, bgn_frame)

                    end_frame = int((note_event['end'] - start_time) * self.frames_per_second)
                    end_frame = min(end_frame, frames_num)

                    bgn_pitch = note_event['pitch'] - self.begin_note

                    if bgn_pitch < self.piano_notes_num:
                        mixture_onset_roll[bgn_frame, bgn_pitch] = 1
                        mixture_frame_roll[bgn_frame : end_frame, bgn_pitch] = 1
                        sep_onset_roll[bgn_frame, bgn_pitch] = 1
                        sep_frame_roll[bgn_frame : end_frame, bgn_pitch] = 1

                        if note_event['start'] < start_time and \
                        (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):
                            mixture_frame_roll[0 : end_frame, bgn_pitch] = 1
                            sep_frame_roll[0 : end_frame, bgn_pitch] = 1

            # print(np.sum(sep_roll))
            if np.sum(sep_onset_roll) > 0:        
                sep_onset_rolls[i] = sep_onset_roll
                sep_frame_rolls[i] = sep_frame_roll
                i += 1

            if i == self.max_instruments_num:
                break

        data_dict['mixture_onset_roll'] = mixture_onset_roll
        data_dict['mixture_frame_roll'] = mixture_frame_roll
        data_dict['sep_onset_rolls'] = sep_onset_rolls
        data_dict['sep_frame_rolls'] = sep_frame_rolls

        return data_dict
'''

'''
class DatasetInstrumentsCluster:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        max_instruments_num,
    ):
        r"""Instrument classification dataset takes the meta of an audio
        segment as input, and return the waveform, onset_roll, and targets of
        the audio segment. Dataset is used by DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
        """
        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.begin_note = BEGIN_NOTE
        self.piano_notes_num = CLASSES_NUM
        self.plugin_lb_to_ix = PLUGIN_LB_TO_IX
        self.max_instruments_num = max_instruments_num

        # random seed
        self.random_state = np.random.RandomState(1234)

    def __getitem__(self, meta: [str, str, float]):
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """

        [split, hdf5_name, start_time] = meta

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, split, hdf5_name)

        data_dict = {}
        # print(hdf5_name, start_time, split)

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:
            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            waveform = int16_to_float32(hf['waveform'][start_sample : end_sample])
            # (segment_samples,), e.g., (160000,)

            if len(waveform) < self.segment_samples:
                valid_length = len(waveform)
                waveform = librosa.util.fix_length(waveform, size=self.segment_samples, axis=0)

            else:
                valid_length = self.segment_samples

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform
            data_dict['valid_length'] = valid_length

        pkl_path = os.path.join(self.notes_pkls_dir, '{}.pkl'.format(pathlib.Path(hdf5_name).stem))
        events_dict = pickle.load(open(pkl_path, 'rb'))

        frames_num = int(self.frames_per_second * self.segment_seconds) + 1

        mixture_onset_roll = np.zeros((frames_num, self.piano_notes_num))
        mixture_frame_roll = np.zeros((frames_num, self.piano_notes_num))
        sep_onset_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))
        sep_frame_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))

        i = 0
        for key in events_dict.keys():
            # ['S00', 'S01', ...]

            # from IPython import embed; embed(using=False); os._exit(0)
            # print(events_dict[key]['program_num'])

            sep_onset_roll = np.zeros((frames_num, self.piano_notes_num))
            sep_frame_roll = np.zeros((frames_num, self.piano_notes_num))

            for note_event in events_dict[key]['note_event']:
                if (note_event['start'] > start_time and note_event['start'] < start_time + self.segment_seconds) or \
                (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):

                    bgn_frame = int((note_event['start'] - start_time) * self.frames_per_second)
                    bgn_frame = max(0, bgn_frame)

                    end_frame = int((note_event['end'] - start_time) * self.frames_per_second)
                    end_frame = min(end_frame, frames_num)

                    bgn_pitch = note_event['pitch'] - self.begin_note

                    if bgn_pitch < self.piano_notes_num:
                        mixture_onset_roll[bgn_frame, bgn_pitch] = 1
                        mixture_frame_roll[bgn_frame : end_frame, bgn_pitch] = 1
                        sep_onset_roll[bgn_frame, bgn_pitch] = 1
                        sep_frame_roll[bgn_frame : end_frame, bgn_pitch] = 1

                        if note_event['start'] < start_time and \
                        (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):
                            mixture_frame_roll[0 : end_frame, bgn_pitch] = 1
                            sep_frame_roll[0 : end_frame, bgn_pitch] = 1

            program_num = events_dict[key]['program_num']
            if program_num in range(32, 40):
                sep_onset_rolls[1] += sep_onset_roll
                sep_frame_rolls[1] += sep_frame_roll
            else:
                sep_onset_rolls[0] += sep_onset_roll
                sep_frame_rolls[0] += sep_frame_roll

        sep_onset_rolls = np.clip(sep_onset_rolls, 0, 1)
        sep_frame_rolls = np.clip(sep_frame_rolls, 0, 1)

        data_dict['mixture_onset_roll'] = mixture_onset_roll
        data_dict['mixture_frame_roll'] = mixture_frame_roll
        data_dict['sep_onset_rolls'] = sep_onset_rolls
        data_dict['sep_frame_rolls'] = sep_frame_rolls
        # print(hdf5_name, start_time)

        return data_dict
''' 

class DatasetInstrumentsCluster:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        max_instruments_num,
    ):
        r"""Instrument classification dataset takes the meta of an audio
        segment as input, and return the waveform, onset_roll, and targets of
        the audio segment. Dataset is used by DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
        """
        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.begin_note = BEGIN_NOTE
        self.piano_notes_num = CLASSES_NUM
        self.plugin_lb_to_ix = PLUGIN_LB_TO_IX
        self.max_instruments_num = max_instruments_num

        # random seed
        self.random_state = np.random.RandomState(1234)

    def __getitem__(self, meta: [str, str, float]):
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """

        [split, hdf5_name, start_time] = meta

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, split, hdf5_name)

        data_dict = {}
        # print(hdf5_name, start_time, split)

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:
            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            waveform = int16_to_float32(hf['waveform'][start_sample : end_sample])
            # (segment_samples,), e.g., (160000,)

            if len(waveform) < self.segment_samples:
                valid_length = len(waveform)
                waveform = librosa.util.fix_length(waveform, size=self.segment_samples, axis=0)

            else:
                valid_length = self.segment_samples

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform
            data_dict['valid_length'] = valid_length

        pkl_path = os.path.join(self.notes_pkls_dir, '{}.pkl'.format(pathlib.Path(hdf5_name).stem))
        events_dict = pickle.load(open(pkl_path, 'rb'))

        frames_num = int(self.frames_per_second * self.segment_seconds) + 1

        mixture_onset_roll = np.zeros((frames_num, self.piano_notes_num))
        mixture_frame_roll = np.zeros((frames_num, self.piano_notes_num))
        # sep_onset_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))
        # sep_frame_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))
        sep_onset_rolls = []
        sep_frame_rolls = []

        i = 0
        tmp = []
        for key in events_dict.keys():
            # ['S00', 'S01', ...]

            # from IPython import embed; embed(using=False); os._exit(0)
            # print(events_dict[key]['program_num'])

            sep_onset_roll = np.zeros((frames_num, self.piano_notes_num))
            sep_frame_roll = np.zeros((frames_num, self.piano_notes_num))

            for note_event in events_dict[key]['note_event']:
                if (note_event['start'] > start_time and note_event['start'] < start_time + self.segment_seconds) or \
                (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):

                    bgn_frame = int((note_event['start'] - start_time) * self.frames_per_second)
                    bgn_frame = max(0, bgn_frame)

                    end_frame = int((note_event['end'] - start_time) * self.frames_per_second)
                    end_frame = min(end_frame, frames_num)

                    bgn_pitch = note_event['pitch'] - self.begin_note

                    if bgn_pitch < self.piano_notes_num:
                        mixture_onset_roll[bgn_frame, bgn_pitch] = 1
                        mixture_frame_roll[bgn_frame : end_frame, bgn_pitch] = 1
                        sep_onset_roll[bgn_frame, bgn_pitch] = 1
                        sep_frame_roll[bgn_frame : end_frame, bgn_pitch] = 1

                        if note_event['start'] < start_time and \
                        (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):
                            mixture_frame_roll[0 : end_frame, bgn_pitch] = 1
                            sep_frame_roll[0 : end_frame, bgn_pitch] = 1

            sep_onset_rolls.append(sep_onset_roll)
            sep_frame_rolls.append(sep_frame_roll)
            tmp.append(np.sum(sep_frame_roll))

        # new_sep_onset_rolls = []
        # new_sep_frame_rolls = []
        new_sep_onset_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))
        new_sep_frame_rolls = np.zeros((self.max_instruments_num, frames_num, self.piano_notes_num))

        locts = np.argsort(tmp)[::-1]

        for i in range(min(self.max_instruments_num, len(sep_frame_rolls))):
            new_sep_onset_rolls[i] = sep_onset_rolls[locts[i]]
            new_sep_frame_rolls[i] = sep_frame_rolls[locts[i]]
            # new_sep_onset_rolls.append(sep_onset_rolls[locts[i]])
            # new_sep_frame_rolls.append(sep_frame_rolls[locts[i]])

        # new_sep_onset_rolls = np.stack(new_sep_onset_rolls, axis=0)
        # new_sep_frame_rolls = np.stack(new_sep_frame_rolls, axis=0)

        # print(events_dict[key]['program_num'], np.sum(sep_frame_roll))
        # if np.sum(sep_onset_roll) > 0:        
        #     sep_onset_rolls[i] = sep_onset_roll
        #     sep_frame_rolls[i] = sep_frame_roll
        #     i += 1

        # if i == self.max_instruments_num:
        #     break

        data_dict['mixture_onset_roll'] = mixture_onset_roll
        data_dict['mixture_frame_roll'] = mixture_frame_roll
        data_dict['sep_onset_rolls'] = new_sep_onset_rolls
        data_dict['sep_frame_rolls'] = new_sep_frame_rolls

        return data_dict



###########
class DataModuleInstrumentsCount(LightningDataModule):
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir: str,
        segment_seconds: float,
        hop_seconds: float,
        frames_per_second: int,
        augmentor: Augmentor,
        max_instruments_num,
        batch_size: int,
        steps_per_epoch: int,
        num_workers: int,
        distributed: bool,
        mini_data: bool,
    ):
        r"""Instrument classification data module.

        Args:
            waveform_hdf5s_dir: str
            notes_pkl_pth: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            classes_num: int, plugins number, e.g., 167
            batch_size: int
            steps_per_epoch: int
            num_workers: int
            distributed: bool
            mini_data: bool, set True to use a small amount of data for debugging
        """
        super().__init__()

        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.hop_seconds = hop_seconds
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch
        self.mini_data = mini_data
        self.num_workers = num_workers
        self.distributed = distributed

        self.train_dataset = DatasetInstrumentsCount(
            waveform_hdf5s_dir=waveform_hdf5s_dir,
            notes_pkls_dir=notes_pkls_dir,
            segment_seconds=segment_seconds,
            frames_per_second=frames_per_second,
            augmentor=augmentor,
            max_instruments_num=max_instruments_num,
        )

    def setup(self, stage: Optional[str] = None):
        r"""called on every device."""

        # SegmentSampler is used for selecting segments for training.
        # On multiple devices, each SegmentSampler samples a part of mini-batch
        # data.
        _train_sampler = SegmentSampler(
            hdf5s_dir=self.waveform_hdf5s_dir,
            split='train',
            segment_seconds=self.segment_seconds,
            hop_seconds=self.hop_seconds,
            batch_size=self.batch_size,
            steps_per_epoch=self.steps_per_epoch,
            evaluation=False,
            mini_data=self.mini_data,
        )

        if self.distributed:
            self.train_sampler = DistributedSamplerWrapper(_train_sampler)
        else:
            self.train_sampler = _train_sampler

    def train_dataloader(self):
        train_loader = torch.utils.data.DataLoader(
            dataset=self.train_dataset,
            batch_sampler=self.train_sampler,
            collate_fn=collate_fn,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return train_loader


class DatasetInstrumentsCount:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
        max_instruments_num,
    ):
        r"""Instrument classification dataset takes the meta of an audio
        segment as input, and return the waveform, onset_roll, and targets of
        the audio segment. Dataset is used by DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
        """
        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.begin_note = BEGIN_NOTE
        self.piano_notes_num = CLASSES_NUM
        self.plugin_lb_to_ix = PLUGIN_LB_TO_IX
        self.max_instruments_num = max_instruments_num

        # random seed
        self.random_state = np.random.RandomState(1234)

    def __getitem__(self, meta: [str, str, float]):
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """

        [split, hdf5_name, start_time] = meta

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, split, hdf5_name)

        data_dict = {}

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:
            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            waveform = int16_to_float32(hf['waveform'][start_sample : end_sample])
            # (segment_samples,), e.g., (160000,)

            if len(waveform) < self.segment_samples:
                valid_length = len(waveform)
                waveform = librosa.util.fix_length(waveform, size=self.segment_samples, axis=0)

            else:
                valid_length = self.segment_samples

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform
            data_dict['valid_length'] = valid_length

        pkl_path = os.path.join(self.notes_pkls_dir, '{}.pkl'.format(pathlib.Path(hdf5_name).stem))
        events_dict = pickle.load(open(pkl_path, 'rb'))

        frames_num = self.frames_per_second * self.segment_seconds + 1

        count = 0
        for key in events_dict.keys():
            for note_event in events_dict[key]['note_event']:
                if (note_event['start'] > start_time and note_event['start'] < start_time + self.segment_seconds) or \
                (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):
                    count += 1
                    break

            if count == self.max_instruments_num - 1:
                break

        select = '2'
        if select == '1':
            target = np.zeros(self.max_instruments_num)
            target[count] = 1
        elif select == '2':
            target = np.array([count])

        
        data_dict['target'] = target


        return data_dict


###############
class DataModuleInstrumentsFilter(LightningDataModule):
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir: str,
        segment_seconds: float,
        hop_seconds: float,
        frames_per_second: int,
        augmentor: Augmentor,
        batch_size: int,
        steps_per_epoch: int,
        num_workers: int,
        distributed: bool,
        mini_data: bool,
    ):
        r"""Instrument classification data module.

        Args:
            waveform_hdf5s_dir: str
            notes_pkl_pth: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
            classes_num: int, plugins number, e.g., 167
            batch_size: int
            steps_per_epoch: int
            num_workers: int
            distributed: bool
            mini_data: bool, set True to use a small amount of data for debugging
        """
        super().__init__()

        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.hop_seconds = hop_seconds
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch
        self.mini_data = mini_data
        self.num_workers = num_workers
        self.distributed = distributed

        self.train_dataset = DatasetInstrumentsFilter(
            waveform_hdf5s_dir=waveform_hdf5s_dir,
            notes_pkls_dir=notes_pkls_dir,
            segment_seconds=segment_seconds,
            frames_per_second=frames_per_second,
            augmentor=augmentor,
        )

    def setup(self, stage: Optional[str] = None):
        r"""called on every device."""

        # SegmentSampler is used for selecting segments for training.
        # On multiple devices, each SegmentSampler samples a part of mini-batch
        # data.
        _train_sampler = SegmentSampler(
            hdf5s_dir=self.waveform_hdf5s_dir,
            split='train',
            segment_seconds=self.segment_seconds,
            hop_seconds=self.hop_seconds,
            batch_size=self.batch_size,
            steps_per_epoch=self.steps_per_epoch,
            evaluation=False,
            mini_data=self.mini_data,
        )

        if self.distributed:
            self.train_sampler = DistributedSamplerWrapper(_train_sampler)
        else:
            self.train_sampler = _train_sampler

    def train_dataloader(self):
        train_loader = torch.utils.data.DataLoader(
            dataset=self.train_dataset,
            batch_sampler=self.train_sampler,
            collate_fn=collate_fn,
            num_workers=self.num_workers,
            pin_memory=True,
        )
        return train_loader


class DatasetInstrumentsFilter:
    def __init__(
        self,
        waveform_hdf5s_dir: str,
        notes_pkls_dir,
        segment_seconds: str,
        frames_per_second: int,
        augmentor: Augmentor,
    ):
        r"""Instrument classification dataset takes the meta of an audio
        segment as input, and return the waveform, onset_roll, and targets of
        the audio segment. Dataset is used by DataLoader.

        Args:
            waveform_hdf5s_dir: str
            midi_events_hdf5s_dir: str
            segment_seconds: float, e.g., 2.0
            frames_per_second: int, e.g., 100
            augmentor: Augmentor
        """
        self.waveform_hdf5s_dir = waveform_hdf5s_dir
        self.notes_pkls_dir = notes_pkls_dir
        self.segment_seconds = segment_seconds
        self.frames_per_second = frames_per_second
        self.augmentor = augmentor
        self.sample_rate = SAMPLE_RATE

        self.segment_samples = int(SAMPLE_RATE * self.segment_seconds)
        self.begin_note = BEGIN_NOTE
        self.piano_notes_num = CLASSES_NUM
        self.plugin_lb_to_ix = PLUGIN_LB_TO_IX

        # random seed
        self.random_state = np.random.RandomState(1234)

        self.individual_stems_hdf5s_dir = '/home/tiger/workspaces/jointist/hdf5s/test9'

        self.target_processor = TargetProcessor(segment_seconds=10,
            frames_per_second=100,
            begin_note=21,
            classes_num=88
        )

    def __getitem__(self, meta: [str, str, float]):
        r"""Get input and target of a segment for training.

        Args:
            meta: list, [split, hdf5_name, start_time], e.g.,
            ['train', 'Track00255.h5', 4.0]

        Returns:
          data_dict: {
            'waveform': (samples_num,)
            'onset_roll': (frames_num, classes_num),
            'offset_roll': (frames_num, classes_num),
            'reg_onset_roll': (frames_num, classes_num),
            'reg_offset_roll': (frames_num, classes_num),
            'frame_roll': (frames_num, classes_num),
            'velocity_roll': (frames_num, classes_num),
            'mask_roll':  (frames_num, classes_num),
            'pedal_onset_roll': (frames_num,),
            'pedal_offset_roll': (frames_num,),
            'reg_pedal_onset_roll': (frames_num,),
            'reg_pedal_offset_roll': (frames_num,),
            'pedal_frame_roll': (frames_num,)}
        """

        [split, hdf5_name, start_time] = meta

        # paths
        waveform_hdf5_path = os.path.join(self.waveform_hdf5s_dir, split, hdf5_name)

        data_dict = {}

        # Load segment waveform.
        with h5py.File(waveform_hdf5_path, 'r') as hf:
            start_sample = int(start_time * self.sample_rate)
            end_sample = start_sample + self.segment_samples

            waveform = int16_to_float32(hf['waveform'][start_sample : end_sample])
            # (segment_samples,), e.g., (160000,)

            if len(waveform) < self.segment_samples:
                valid_length = len(waveform)
                waveform = librosa.util.fix_length(waveform, size=self.segment_samples, axis=0)

            else:
                valid_length = self.segment_samples

            if self.augmentor:
                waveform = self.augmentor(waveform)

            data_dict['waveform'] = waveform
            data_dict['valid_length'] = valid_length

        pkl_path = os.path.join(self.notes_pkls_dir, '{}.pkl'.format(pathlib.Path(hdf5_name).stem))
        events_dict = pickle.load(open(pkl_path, 'rb'))

        frames_num = self.frames_per_second * self.segment_seconds + 1

        # count = 0
        keys = []
        indexes = []
        plugin_names = []

        for i, key in enumerate(events_dict.keys()):

            keys.append(key)
            plugin_names.append(events_dict[key]['plugin_name'])

            for note_event in events_dict[key]['note_event']:

                if (note_event['start'] > start_time and note_event['start'] < start_time + self.segment_seconds) or \
                (note_event['end'] > start_time and note_event['end'] < start_time + self.segment_seconds):
                    # count += 1
                    # keys.append(key)
                    indexes.append(i)
                    break

        if len(indexes) == 0:
            index = 0
        else:
            index = self.random_state.choice(indexes, size=1)[0]

        plugin_target = np.zeros(PLUGIN_LABELS_NUM)
        plugin_target[PLUGIN_LB_TO_IX[plugin_names[index]]] = 1

        hdf5_path = os.path.join(self.individual_stems_hdf5s_dir, split, pathlib.Path(hdf5_name).stem, '{}.h5'.format(keys[index]))

        with h5py.File(hdf5_path, 'r') as hf:
            midi_events = [e.decode() for e in hf['0']['midi_event'][:]]
            midi_events_time = hf['0']['midi_event_time'][:]


        target_dict, note_events, pedal_events = self.target_processor.process(
            start_time=start_time, 
            midi_events_time=midi_events_time, 
            midi_events=midi_events
        )
        
        data_dict['plugin_target'] = plugin_target

        for key in target_dict.keys():
            data_dict[key] = target_dict[key]


        return data_dict


############
def plot_waveform_midi_targets(data_dict, start_time, note_events):
    """For debugging. Write out waveform, MIDI and plot targets for an
    audio segment.

    Args:
      data_dict: {
        'waveform': (samples_num,),
        'onset_roll': (frames_num, classes_num),
        'offset_roll': (frames_num, classes_num),
        'reg_onset_roll': (frames_num, classes_num),
        'reg_offset_roll': (frames_num, classes_num),
        'frame_roll': (frames_num, classes_num),
        'velocity_roll': (frames_num, classes_num),
        'mask_roll':  (frames_num, classes_num),
        'reg_pedal_onset_roll': (frames_num,),
        'reg_pedal_offset_roll': (frames_num,),
        'pedal_frame_roll': (frames_num,)}
      start_time: float
      note_events: list of dict, e.g. [
        {'midi_note': 51, 'onset_time': 696.63544, 'offset_time': 696.9948, 'velocity': 44},
        {'midi_note': 58, 'onset_time': 696.99585, 'offset_time': 697.18646, 'velocity': 50}
    """
    import matplotlib.pyplot as plt
    import soundfile
    import librosa

    os.makedirs('debug', exist_ok=True)
    audio_path = 'debug/debug.wav'
    midi_path = 'debug/debug.mid'
    fig_path = 'debug/debug.pdf'

    soundfile.write(file=audio_path, data=data_dict['waveform'], samplerate=16000)
    # librosa.output.write_wav(audio_path, data_dict['waveform'], sr=config.sample_rate)
    # write_events_to_midi(start_time, note_events, None, midi_path)
    x = librosa.core.stft(y=data_dict['waveform'], n_fft=2048, hop_length=160, window='hann', center=True)
    x = np.abs(x) ** 2

    fig, axs = plt.subplots(3, 1, sharex=True, figsize=(10, 8))
    fontsize = 20
    axs[0].matshow(np.log(x), origin='lower', aspect='auto', cmap='jet')
    # axs[1].matshow(data_dict['0_frame_roll'].T, origin='lower', aspect='auto', cmap='jet')
    axs[2].matshow(data_dict['percussion_frame_roll'].T, origin='lower', aspect='auto', cmap='jet')
    plt.tight_layout(1, 1, 1)
    plt.savefig(fig_path)

    print('Write out to {}, {}, {}!'.format(audio_path, midi_path, fig_path))
    from IPython import embed

    embed(using=False)
    os._exit(0)
