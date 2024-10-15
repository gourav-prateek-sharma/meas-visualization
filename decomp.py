import os, sys, gzip, json
from pathlib import Path
from loguru import logger
import pandas as pd
from edaf.core.uplink.preprocess import preprocess_ul
from edaf.core.uplink.analyze_packet import ULPacketAnalyzer
import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np

def get_scheduling_delay(send_ts, sched_arr, slots_per_frame=20, slots_duration_ms=0.5):
    for j in range(0, len(sched_arr)):
        if sched_arr[j]['schedule_ts']>send_ts and sched_arr[j]['schedule_ts']-send_ts<slots_per_frame*slots_duration_ms:
            return (sched_arr[j]['schedule_ts']-send_ts)*1000
    return None

def get_frame_alignment_delay(send_ts, bsrupd_arr, slots_per_frame=20, slots_duration_ms=0.5):
    for j in range(0, len(bsrupd_arr)):
        if bsrupd_arr[j]['timestamp']>send_ts and bsrupd_arr[j]['timestamp']-send_ts<slots_per_frame*slots_duration_ms:
            return (bsrupd_arr[j]['timestamp']-send_ts)*1000
    return None
    
# return queueing delay in millisecons
def get_queueing_delay(packet):
    min_delay = np.inf
    for rlc_seg in packet['rlc.attempts']:
        if rlc_seg.get('mac.in_t')!=None and packet.get('ip.in_t')!=None and packet.get('rlc.attempts')!=None:
            min_delay = min(min_delay, rlc_seg['mac.in_t']-packet['ip.in_t'])
        else:
            logger.error(f"Packet {packet['id']} Either mac.in_t, ip.in_t or rlc.attempts not present")
            return None
    return min_delay*1000
            
# return ran delay in millisecons
def get_ran_delay(packet):
    if packet.get('ip.out_t')!=None and packet.get('ip.in_t')!=None:
        return (packet['ip.out_t']-packet['ip.in_t'])*1000
    else:
        logger.error(f"Packet {packet['id']} either ip.in_t or ip.out_t not present")
        return None

def get_retx_delay_seg(packet, rlc_seg):
    max_delay, min_delay = 0, np.inf
    for mac_attempt in  rlc_seg['mac.attempts']:
        if mac_attempt.get('phy.out_t')!=None:
            max_delay = max(max_delay, mac_attempt['phy.out_t'])
            min_delay = min(min_delay, mac_attempt['phy.out_t'])
        else:
            logger.error(f"Packet {packet['id']} phy.out_t not present")
            return None
    return (max_delay-min_delay)*1000

def get_retx_delay(packet):
    max_delay = 0
    for rlc_seg in packet['rlc.attempts']:
        if len(rlc_seg['mac.attempts'])>0 and get_retx_delay_seg(packet, rlc_seg)!=None:
            max_delay = max(get_retx_delay_seg(packet, rlc_seg), max_delay)
        else:
            logger.error(f"Packet {packet['id']} mac.attempts not present")
            return None
    return max_delay