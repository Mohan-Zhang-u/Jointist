import hydra
from hydra.utils import to_absolute_path
import sparse
import h5py
import numpy as np
import pickle
from End2End.target_processors import TargetProcessor
from pathlib import Path
import tqdm
from hydra.utils import to_absolute_path
import os
import sys
from End2End.MIDI_program_map import (
                                      MIDI_Class_NUM,
                                      MIDIClassName2class_idx,
                                      class_idx2MIDIClass,
                                      )
import torch
import torchaudio

# constants
frames_per_second=100
SAMPLE_RATE=16000



@hydra.main(config_path="End2End/config/", config_name="pkl2sparsepianoroll_MSD")
def main(cfg):
    
    pkl_path = to_absolute_path(cfg.pkl_path)
    cfg.roll_output_path = to_absolute_path(cfg.roll_output_path) 
    Path(cfg.roll_output_path).mkdir(parents=True, exist_ok=True)
 
    
    
    target_processor = TargetProcessor(frames_per_second=frames_per_second,
                                       begin_note=21,
                                       classes_num=88)
    
    pkl_list = list(Path(pkl_path).glob('*.pkl'))
    for pkl_path in tqdm.tqdm(sorted(pkl_list)):
        piece_name = pkl_path.name[:-4]
        note_event = pickle.load(open(pkl_path, 'rb'))
#         valid_length = len(h5[piece_name][()])
#             audio, _ = torchaudio.load(to_absolute_path(f'../MTAT/{piece_name}'))
        valid_length = 16000*30
        segment_seconds = valid_length/SAMPLE_RATE


        flat_frame_roll = event2roll(0,
                                     segment_seconds,
                                     note_event,
                                     target_processor)
        
        # The native torch is having a bug for bool
        # https://github.com/pytorch/pytorch/issues/49977
#         sparse_roll  = torch.tensor(flat_frame_roll).to_sparse()
#         torch.save(sparse_roll, os.path.join(cfg.roll_output_path, f'''{piece_name.replace("['", "").replace("']", "")}.pt'''))
        sparse_roll = sparse.COO(flat_frame_roll)
        sparse.save_npz(os.path.join(cfg.roll_output_path,
                                     f'''{piece_name.replace("['", "").replace("']", "")}'''),
                        sparse_roll)

    
def event2roll(start_time, segment_seconds, note_events, target_processor):
    keys = list(note_events.keys())
    key = keys[0]
    target_dict_per_plugin = target_processor.pkl2roll(start_time=0, 
                                                                    segment_seconds=segment_seconds,
                                                                    note_events=note_events[key],
                                                                   )
    frame_roll = target_dict_per_plugin['frame_roll']
    placeholder = np.zeros_like(frame_roll).astype('bool')
    placeholder = np.expand_dims(placeholder,0)
    placeholder = placeholder.repeat(39,0)
    
    placeholder[MIDIClassName2class_idx[key]] = frame_roll
    for key in keys[1:]:
        target_dict_per_plugin = target_processor.pkl2roll(start_time=0, 
                                                                        segment_seconds=segment_seconds,
                                                                        note_events=note_events[key],
                                                                       )    
        placeholder[MIDIClassName2class_idx[key]] = target_dict_per_plugin['frame_roll']
        
    return placeholder






if __name__ == '__main__':
    main()
