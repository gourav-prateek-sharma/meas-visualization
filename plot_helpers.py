# prompt: open a .parquet file and load the database into a dataframe
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
import numpy as np
import matplotlib.pyplot as plt
from statsmodels.tsa.stattools import acf
import seaborn as sns



if not os.getenv('DEBUG'):
    logger.remove()
    logger.add(sys.stdout, level="INFO")

def find_harq_attempts(ue_rlc_row, ue_mac_attempts_df, packet_delivery_ts):

    # find the first attempt's frame, slot, and hqpid
    fm = ue_rlc_row['mac.sdu.frame']
    sl = ue_rlc_row['mac.sdu.slot']
    m2buf = ue_rlc_row['mac.sdu.M2buf']
    m2len = ue_rlc_row['mac.sdu.length']

    # (m2buf_value >= m3buf_value) and ((m2buf_value + m2len) <= (m3buf_value + m3len)
    # there must be at least one entry in ue mac attempts with this info
    poss_mac_attempt_0s = ue_mac_attempts_df[
        (ue_mac_attempts_df['phy.tx.fm'] == fm) &
        (ue_mac_attempts_df['phy.tx.sl'] == sl) &
        (ue_mac_attempts_df['mac.harq.M3buf'] <= m2buf) &
        ((m2buf+m2len) <= (ue_mac_attempts_df['mac.harq.M3buf']+ue_mac_attempts_df['mac.harq.len'])) &
        (ue_mac_attempts_df['phy.tx.timestamp'] >= ue_rlc_row['rlc.txpdu.timestamp']) &
        (ue_mac_attempts_df['phy.tx.timestamp'] <= packet_delivery_ts)
    ]
    if poss_mac_attempt_0s.shape[0] == 0:
        logger.error("No harq attempts found")
        return
    elif poss_mac_attempt_0s.shape[0] > 1:
        logger.error(f"Multiple harq attempts found: {poss_mac_attempt_0s.shape[0]}.")
        return
    
    mac_attempt_0 = poss_mac_attempt_0s.iloc[0]
    # logger.info(f"Found harq attempt: {mac_attempt_0}")

    hq = mac_attempt_0['phy.tx.hqpid']
    at_0_ts = float(mac_attempt_0['phy.tx.timestamp'])

    # find all attempts with this hq
    hq_attempts = ue_mac_attempts_df[
        (ue_mac_attempts_df['phy.tx.hqpid'] == hq) &
        (ue_mac_attempts_df['phy.tx.timestamp'] > at_0_ts)
    ]
    sorted_hq_attempts = hq_attempts.sort_values(by='phy.tx.timestamp', ascending=True, inplace=False)
    first_ndi_row = sorted_hq_attempts[sorted_hq_attempts['mac.harq.ndi'] == 1]
    #first_ndi1_attempt_ts = float(first_ndi_row.head(1)['phy.tx.timestamp'])
    first_ndi1_attempt_ts = float(first_ndi_row['phy.tx.timestamp'].iloc[0])

    # find all attempts with timestamps less than this
    result = ue_mac_attempts_df[
        (ue_mac_attempts_df['phy.tx.hqpid'] == hq) &
        (ue_mac_attempts_df['phy.tx.timestamp'] < first_ndi1_attempt_ts) &
        (ue_mac_attempts_df['phy.tx.timestamp'] >= at_0_ts)
    ]
    return result, hq

def plot_packet_tree(gnb_ip_row, gnb_rlc_rows, gnb_rlc_segments_df, gnb_mac_attempts_df, ue_ip_row, ue_rlc_rows, ue_mac_attempts_df, prev_input_ip_ts, ax, plot):
    
    packet = {
        'sn' : None,
        'ip.in_t' : None,
        'ip.out_t' : None,
        'rlc.in_t' : None,
        'rlc.out_t' : None,
        'rlc.attempts' : [],
    }

    packet['sn'] = gnb_ip_row['gtp.out.sn']
    packet['ip.in_t'] = float(ue_ip_row['ip.in.timestamp'])
    packet['ip.out_t'] = float(gnb_ip_row['gtp.out.timestamp'])
    packet['rlc.in_t'] = float(ue_ip_row['rlc.queue.timestamp'])

    # Get the number of rlc segments
    num_rlc_segments = len(gnb_rlc_rows)
    logger.info(f"Number of gnb RLC segments {num_rlc_segments}")

    # Get the number of rlc attempts
    num_rlc_attempts = len(ue_rlc_rows)
    logger.info(f"Number of ue RLC attempts {num_rlc_attempts}")

    # Iterate over each rlc attempt
    for i in range(num_rlc_attempts):
        rlcattempt = {
            'mac.in_t' : None,
            'mac.out_t' : None,
            'acked' : False,
            'mac.attempts' : [],
        }
        rlcattempt['mac.in_t'] = ue_rlc_rows[i]['rlc.txpdu.timestamp']

        # frame and slot number of the first harq attempt
        fm_0 = int(ue_rlc_rows[i]['mac.sdu.frame'])
        sl_0 = int(ue_rlc_rows[i]['mac.sdu.slot'])
        
        # Try to find mac attempts for this ue rlc attempt
        harq_attempts, hq_0 = find_harq_attempts(ue_rlc_rows[i], ue_mac_attempts_df, packet['ip.out_t'])
        num_harq_attempts = harq_attempts.shape[0]
        logger.info(f"UE RLC attempt {i} - number of harq attempts discovered: {num_harq_attempts}")
        # frame and slot number of the last harq attempt
        hq_s = None
        fm_s = None
        sl_s = None
        for j in range(num_harq_attempts):
            macattempt = {
                'phy.in_t' : None,
                'phy.out_t' : None,
                'acked' : False
            }
            
            ue_harq_attempt = harq_attempts.iloc[j]
            macattempt['phy.in_t'] = float(ue_harq_attempt[f'phy.tx.timestamp'])

            # now we can find the corresponding mac attempt on gnb side
            gnb_mac_attempt_arr = gnb_mac_attempts_df[
                (gnb_mac_attempts_df['phy.detectend.frame'] == ue_harq_attempt['phy.tx.fm']) &
                (gnb_mac_attempts_df['phy.detectend.slot'] == ue_harq_attempt['phy.tx.sl']) &
                (gnb_mac_attempts_df['phy.detectend.hqpid'] == ue_harq_attempt['phy.tx.hqpid'])
            ]
            if gnb_mac_attempt_arr.shape[0] == 0:
                # unsuccessful harq attempt 
                pass
            elif gnb_mac_attempt_arr.shape[0] > 1:
                logger.warning(f"UE RLC attempt {i} - ue mac attempt {j}, looking for the corresponding gnb mac attempt. Found {gnb_mac_attempt_arr.shape[0]} (more than one) possible gnb mac attempt matches. We pick the one between packet arrival and departure times.")
                for k in range(gnb_mac_attempt_arr.shape[0]):
                    gnb_pot_mac_attempt = gnb_mac_attempt_arr.iloc[k]
                    if gnb_pot_mac_attempt['phy.decodeend.timestamp'] >= packet['ip.in_t'] and gnb_pot_mac_attempt['phy.decodeend.timestamp'] <= packet['ip.out_t']:
                        gnb_mac_attempt = gnb_pot_mac_attempt
            else:
                gnb_mac_attempt = gnb_mac_attempt_arr.iloc[0]

            if pd.isna(gnb_mac_attempt['phy.decodeend.timestamp']):
                # unsuccessful harq attempt 
                pass
            else:
                # possibly successful harq attempt
                macattempt['phy.out_t'] = float(gnb_mac_attempt['phy.decodeend.timestamp'])
                hq_s = int(gnb_mac_attempt['phy.detectend.hqpid'])
                fm_s = int(gnb_mac_attempt['phy.detectend.frame'])
                sl_s = int(gnb_mac_attempt['phy.detectend.slot'])

            # find gnb side of this rlc segment
            # use hq_s, fm_s, and sl_s which belong to the last mac attempt
            # the possible hq, fm, and sl of that rlc segment in gnb
            gnb_rlc_segment_arr = gnb_rlc_segments_df[
                (gnb_rlc_segments_df['rlc.decoded.frame'] == fm_s) &
                (gnb_rlc_segments_df['rlc.decoded.slot'] == sl_s) &
                (gnb_rlc_segments_df['rlc.decoded.hqpid'] == hq_s)
            ]

            if gnb_rlc_segment_arr.shape[0] == 1:
                gnb_rlc_segment = gnb_rlc_segment_arr.iloc[0]
                rlcattempt['mac.out_t'] = gnb_rlc_segment['rlc.reassembled.timestamp']
                rlcattempt['acked'] = True
            elif gnb_rlc_segment_arr.shape[0] > 1:
                logger.warning(f"UE RLC attempt {i} - found {gnb_rlc_segment_arr.shape[0]} (more than one) possible gnb rlc segment matches. We pick the one between packet arrival and departure times.")
                for k in range(gnb_rlc_segment_arr.shape[0]):
                    pot_gnb_seg = gnb_rlc_segment_arr.iloc[k]
                    if pot_gnb_seg['rlc.reassembled.timestamp'] <= packet['ip.out_t'] and pot_gnb_seg['rlc.reassembled.timestamp'] >= packet['ip.in_t']:
                        gnb_rlc_segment = pot_gnb_seg
                        rlcattempt['mac.out_t'] = gnb_rlc_segment['rlc.reassembled.timestamp']
                        rlcattempt['acked'] = True

            rlcattempt['mac.attempts'].append(macattempt)

        # sort harq attempts based on their timestamp
        rlcattempt['mac.attempts'] = sorted(rlcattempt['mac.attempts'], key=lambda x: x['phy.in_t'])

        # append the rlc segment
        packet['rlc.attempts'].append(rlcattempt)

    # sort rlc segments based on their timestamp
    packet['rlc.attempts'] = sorted(packet['rlc.attempts'], key=lambda x: x['mac.in_t'])

    # fix acked flag for harq attempts
    for rlcattempt in packet['rlc.attempts']:
        if rlcattempt['acked']:
            for id, mac_att in enumerate(rlcattempt['mac.attempts']):
                if id < len(rlcattempt['mac.attempts'])-1:
                    mac_att['acked'] = False
                else:
                    mac_att['acked'] = True
        else:
            for id, mac_att in enumerate(rlcattempt['mac.attempts']):
                    mac_att['acked'] = False

    # fix rlc.out_t, which is the latest rlc.attempts
    rlc_out = 0
    for i in range(len(packet['rlc.attempts'])):
        if packet['rlc.attempts'][i]['mac.out_t']:
            rlc_out = max(packet['rlc.attempts'][i]['mac.out_t'],rlc_out)
    packet['rlc.out_t'] = rlc_out
    #print(packet)

    if not plot:
        return packet['ip.in_t'] 

    # Starting point
    x_start, y_start = 0, 0

    # plot
    if prev_input_ip_ts == 0:
        x_os = 0
    else:
        x_os = (packet['ip.in_t'] - prev_input_ip_ts)*1000
    x_start = x_start + x_os

    branch0_x = x_start + (packet['rlc.in_t'] - packet['ip.in_t'])*1000
    branch0_y = y_start + 1
    ax.plot([x_start, branch0_x], [y_start, branch0_y], color='green')

    # Iterate over each attempt
    num_rlc_attempts = len(packet['rlc.attempts'])
    for i in range(num_rlc_attempts):
        tx_pdu_timestamp = packet['rlc.attempts'][i]['mac.in_t']
        branch1_x = branch0_x + (tx_pdu_timestamp - packet['rlc.in_t'])*1000
        branch1_y = branch0_y + 1

        if not packet['rlc.attempts'][i]['acked']:
          # this rlc segment could be nacked, check it TODO
          # plot the rlc segment line
          ax.plot([branch0_x, branch1_x], [branch0_y, branch1_y], color='red')
          continue 
        else:
          # plot the rlc segment line
          ax.plot([branch0_x, branch1_x], [branch0_y, branch1_y], color='blue')

        for j in range(len(packet['rlc.attempts'][i]['mac.attempts'])):
            if packet['rlc.attempts'][i]['mac.attempts'][j]['acked']:
                # ue side mac
                branch2_x = branch1_x + (
                        packet['rlc.attempts'][i]['mac.attempts'][j]['phy.in_t']-tx_pdu_timestamp
                    )*1000
                branch2_y = branch1_y + 1
                ax.plot([branch1_x, branch2_x], [branch1_y, branch2_y], color='orange')

                # gnb/ue side phy
                branch3_x = branch2_x + (
                    packet['rlc.attempts'][i]['mac.attempts'][j]['phy.out_t'] - packet['rlc.attempts'][i]['mac.attempts'][j]['phy.in_t']
                )*1000
                branch3_y = branch2_y + 1
                ax.plot([branch2_x, branch3_x], [branch2_y, branch3_y], color='orange')

                # gnb side mac
                branch4_x = branch3_x + (
                        packet['rlc.attempts'][i]['mac.out_t']-packet['rlc.attempts'][i]['mac.attempts'][j]['phy.out_t']
                    )*1000
                branch4_y = branch3_y + 1
                ax.plot([branch3_x, branch4_x], [branch3_y, branch4_y], color='orange') 

            else:
                # only ue side, in grey
                branch2_x = branch1_x + (
                        packet['rlc.attempts'][i]['mac.attempts'][j]['phy.in_t']-packet['rlc.attempts'][i]['mac.in_t']
                    )*1000
                branch2_y = branch1_y + 1
                ax.plot([branch1_x, branch2_x], [branch1_y, branch2_y], color='red') 

    branch5_x = branch4_x + (packet['ip.out_t']-packet['rlc.out_t'])*1000
    branch5_y = branch4_y + 1
    ax.plot([branch4_x, branch5_x], [branch4_y, branch5_y], color='green')   

    return packet['ip.in_t']       

def figure_out_grid(map_row):
    INUMS = 4
    PRBS = 106
    SYMS = 14

    # find RBs structure
    ins_pr = []
    ins_po = []
    for i in range(INUMS):
        ins_pr.append(int(map_row[f'sched.map.pr.i{INUMS-i-1}m']))
        ins_po.append(int(map_row[f'sched.map.po.i{INUMS-i-1}m']))
    
    binary_string = ''.join(format(num, '016b') for num in ins_pr)[::-1]
    first_106_bits = binary_string[:PRBS]
    pr_bit_list = [int(bit) for bit in first_106_bits]

    binary_string = ''.join(format(num, '016b') for num in ins_po)[::-1]
    first_106_bits = binary_string[:PRBS]
    po_bit_list = [int(bit) for bit in first_106_bits]

    return pr_bit_list, po_bit_list


def plot_resourcegrid(begin_ts, end_ts, gnb_sched_maps_df, ax):

    NUM_PRBS = 106
    SYMS_PER_SLOT = 14
    SLOTS_PER_FRAME = 20
    SLOT_LENGTH = 0.5 #ms

    # bring all sched.map.pr and sched.map.po within this frame
    maps = gnb_sched_maps_df[
        (gnb_sched_maps_df['sched.map.pr.timestamp'] >= begin_ts-0.010) &
        (gnb_sched_maps_df['sched.map.pr.timestamp'] < end_ts)
    ]
    for i in range(maps.shape[0]):
        map_row = maps.iloc[i]
        map_ts = map_row['sched.map.pr.timestamp']
        dl_slot_time = (map_ts-begin_ts)*1000+SLOT_LENGTH*4
        #rect = patches.Rectangle((dl_slot_time-(SLOT_LENGTH/2), 3), SLOT_LENGTH, 1, color='blue')
        #ax.add_patch(rect)

        # find RBs structure
        pr_bit_list, po_bit_list = figure_out_grid(map_row)
        blocked_bits_list = [bit1 & bit2 for bit1, bit2 in zip(pr_bit_list, po_bit_list)]

        # find symbols structure
        sym_begin = int(map_row[f'sched.map.po.sb'])
        sym_size = int(map_row[f'sched.map.po.ss'])
        # find fm, sl and fmtx, sltx
        abs_sltx_po = int(map_row[f'sched.map.po.frametx'])*SLOTS_PER_FRAME +int(map_row[f'sched.map.po.slottx']) 
        abs_sl_po = int(map_row[f'sched.map.po.frame'])*SLOTS_PER_FRAME +int(map_row[f'sched.map.po.slot'])
        sltx_tsdif_ms = (abs_sltx_po - abs_sl_po)*SLOT_LENGTH

        offset = sym_begin/SYMS_PER_SLOT*SLOT_LENGTH
        width = sym_size/SYMS_PER_SLOT*SLOT_LENGTH
        #rect = patches.Rectangle((dl_slot_time+sltx_tsdif_ms+offset, 3), width, 1, color='green')
        #ax.add_patch(rect)



        
        #tot_height = 1
        #height = tot_height/NUM_PRBS*
        #rect = patches.Rectangle((dl_slot_time+sltx_tsdif_ms+offset, 3), width, 1, color='purple')
        #ax.add_patch(rect)

        tot_height = 1
        segment_height = tot_height / len(blocked_bits_list)
        for i, bit in enumerate(blocked_bits_list):
            color = 'grey' if bit == 1 else 'green'
            y_pos = i * segment_height 
            rect = patches.Rectangle((dl_slot_time+sltx_tsdif_ms+offset, 3+y_pos), width, segment_height, color=color)
            ax.add_patch(rect)

def append_if_not_close(new_number, numbers, delta):
    for num in numbers:
        if abs(num - new_number) < delta:
            return  # Close number exists, so don't append
    numbers.append(new_number)

def plot_sched_tree(begin_ts, end_ts, ue_srtxs_df, ue_bsrupds_df, ue_bsrtxs_df, ue_uldcis_df, gnb_sched_reports_df, ax):

    NUM_PRBS = 106
    SYMS_PER_SLOT = 14
    SLOTS_PER_FRAME = 20
    SLOT_LENGTH = 0.5 #ms
    MAX_QUEUE_SIZE = 1000

    # bring all bsr.upd within this frame
    # find bsr updates transmitted 'bsr.tx'
    bsr_upd_list = ue_bsrupds_df[
        (ue_bsrupds_df['timestamp'] >= begin_ts) &
        (ue_bsrupds_df['timestamp'] < end_ts)
    ]
    if bsr_upd_list.shape[0] == 0:
        logger.warning("Did not find any bsr report for this interval.")

    bsr_upd_rows = []
    for i in range(bsr_upd_list.shape[0]):
        bsr_upd_row = bsr_upd_list.iloc[i]
        bsr_upd_rows.append(bsr_upd_row)

    sorted_bsr_upd_rows = sorted(bsr_upd_rows, key=lambda x: x['timestamp'])

    for i in range(len(sorted_bsr_upd_rows)):
        bsr_upd_row = sorted_bsr_upd_rows[i]
        # 'frame', 'slot', 'timestamp', 'lcid', 'bsri', 'len'
        width = 10
        tot_height = 0.5
        full_segment_height = tot_height / MAX_QUEUE_SIZE * bsr_upd_row['len']
        empty_segment_height = tot_height - full_segment_height
        x_pos = (bsr_upd_row['timestamp'] - begin_ts)*1000
        y_pos = 1
        rect = patches.Rectangle(
            (
                x_pos, 
                y_pos,
            ),
            width, 
            full_segment_height, 
            color='red'
        )
        ax.add_patch(rect)
        rect = patches.Rectangle(
            (
                x_pos,
                y_pos+full_segment_height
            ),
            width, 
            empty_segment_height, 
            color='grey'
        )
        ax.add_patch(rect)


    # bring all bsr.tx within this frame
    # find bsr updates transmitted 'bsr.tx'
    bsrtx_list = ue_bsrtxs_df[
        (ue_bsrtxs_df['timestamp'] >= begin_ts) &
        (ue_bsrtxs_df['timestamp'] < end_ts)
    ]
    if bsrtx_list.shape[0] == 0:
        logger.warning("Did not find any bsr report for this interval.")

    for i in range(bsrtx_list.shape[0]):
        bsrts_row = bsrtx_list.iloc[i]
        # 'frame', 'slot', 'timestamp', 'lcid', 'bsri', 'len'
        width = 0.1
        tot_height = 0.5
        full_segment_height = tot_height / MAX_QUEUE_SIZE * bsrts_row['len']
        empty_segment_height = tot_height - full_segment_height
        x_pos = (bsrts_row['timestamp'] - begin_ts)*1000
        y_pos = 1.5
        rect = patches.Rectangle(
            (
                x_pos, 
                y_pos,
            ),
            width, 
            full_segment_height, 
            color='red'
        )
        ax.add_patch(rect)
        rect = patches.Rectangle(
            (
                x_pos,
                y_pos+full_segment_height
            ),
            width, 
            empty_segment_height, 
            color='grey'
        )
        ax.add_patch(rect)


    # bring all sr.tx within this frame
    # find bsr updates transmitted 'sr.tx'
    srtx_list = ue_srtxs_df[
        (ue_srtxs_df['timestamp'] >= begin_ts) &
        (ue_srtxs_df['timestamp'] < end_ts)
    ]
    if srtx_list.shape[0] == 0:
        logger.warning("Did not find any bsr report for this interval.")

    for i in range(srtx_list.shape[0]):
        srtx_row = srtx_list.iloc[i]
        # 'frame', 'slot', 'timestamp', 'lcid', 'bsri', 'len'
        width = 0.1
        height = 0.5
        x_pos = (srtx_row['timestamp'] - begin_ts)*1000
        y_pos = 1.5
        rect = patches.Rectangle(
            (
                x_pos, 
                y_pos,
            ),
            width, 
            height, 
            color='red'
        )
        ax.add_patch(rect)

    # bring all sched.ue within this frame
    sched_ue_list = gnb_sched_reports_df[
        (gnb_sched_reports_df['sched.ue.timestamp'] >= begin_ts-0.010) &
        (gnb_sched_reports_df['sched.ue.timestamp'] < end_ts)
    ]
    for i in range(sched_ue_list.shape[0]):
        # find sched.ue events
        ue_sched_row = sched_ue_list.iloc[i]
        ue_sched_ts = ue_sched_row['sched.ue.timestamp']

        # what is its cause?
        if ue_sched_row['sched.cause.type'] > 0:
            if int(ue_sched_row['sched.cause.type']) == 1:
                # due to bsr
                width = 0.1
                height = 0.5
                x_pos = (ue_sched_row['sched.ue.timestamp'] - begin_ts)*1000
                y_pos = 2
                rect = patches.Rectangle(
                    (
                        x_pos, 
                        y_pos,
                    ),
                    width, 
                    height, 
                    color='green'
                )
                ax.add_patch(rect)

            elif int(ue_sched_row['sched.cause.type']) == 2:
                # due to sr
                width = 0.1
                height = 0.5
                x_pos = (ue_sched_row['sched.ue.timestamp'] - begin_ts)*1000
                y_pos = 2
                rect = patches.Rectangle(
                    (
                        x_pos, 
                        y_pos,
                    ),
                    width, 
                    height, 
                    color='orange'
                )
                ax.add_patch(rect)
            elif int(ue_sched_row['sched.cause.type']) == 3:
                # no activity, we dont do anything for this
                # due to non activity
                width = 0.1
                height = 0.5
                x_pos = (ue_sched_row['sched.ue.timestamp'] - begin_ts)*1000
                y_pos = 2
                rect = patches.Rectangle(
                    (
                        x_pos, 
                        y_pos,
                    ),
                    width, 
                    height, 
                    color='yellow'
                )
                ax.add_patch(rect)

            ue_bsr_ts_no_os = (ue_sched_ts-begin_ts)*1000+SLOT_LENGTH*4
            width = 0.25
            height = 1
            # find fm, sl and fmtx, sltx
            abs_sltx_po = int(ue_sched_row[f'sched.ue.frametx'])*SLOTS_PER_FRAME +int(ue_sched_row[f'sched.ue.slottx'])
            abs_sl_po = int(ue_sched_row[f'sched.ue.frame'])*SLOTS_PER_FRAME +int(ue_sched_row[f'sched.ue.slot'])
            sltx_tsdif_ms = (abs_sltx_po - abs_sl_po)*SLOT_LENGTH
            x1_pos = x_pos
            y1_pos = 2.5
            x2_pos = ue_bsr_ts_no_os+sltx_tsdif_ms
            y2_pos = 3
            ax.plot([x1_pos, x2_pos], [y1_pos, y2_pos], color='orange') 
            

            # ul dci
            uldci_list = ue_uldcis_df[
                (ue_uldcis_df['timestamp'] >= begin_ts) &
                (ue_uldcis_df['timestamp'] < end_ts)
            ]
            if uldci_list.shape[0] == 0:
                logger.warning("Did not find any uldci_list for this interval.")

            for i in range(uldci_list.shape[0]):
                uldci_row = uldci_list.iloc[i]
                # 'frame', 'slot', 'timestamp', 'lcid', 'bsri', 'len'
                width = 0.1
                height = 0.5
                x_pos = (uldci_row['timestamp'] - begin_ts)*1000
                y_pos = 4.5
                rect = patches.Rectangle(
                    (
                        x_pos, 
                        y_pos,
                    ),
                    width, 
                    height, 
                    color='blue'
                )
                ax.add_patch(rect)

                # find fm, sl and fmtx, sltx
                ue_bsr_ts_no_os = (uldci_row['timestamp']-begin_ts)*1000 #+SLOT_LENGTH*4
                abs_sltx_po = int(uldci_row['frametx'])*SLOTS_PER_FRAME +int(uldci_row['slottx'])
                abs_sl_po = int(uldci_row[f'frame'])*SLOTS_PER_FRAME +int(uldci_row[f'slot'])
                sltx_tsdif_ms = (abs_sltx_po - abs_sl_po)*SLOT_LENGTH
                x1_pos = x_pos
                y1_pos = 4.5
                x2_pos = ue_bsr_ts_no_os+sltx_tsdif_ms
                y2_pos = 4
                ax.plot([x1_pos, x2_pos], [y1_pos, y2_pos], color='orange')

    return


def plot_tree_from_sns(sn_list : list, ax):
   
    # find interval
    end_ts = 0
    begin_ts = np.inf
    input_ip_ts = 0
    for sn in sn_list:
        gnb_ip_row = gnb_ip_packets_df[gnb_ip_packets_df['gtp.out.sn'] == sn].iloc[0]
        filtered_df = gnb_iprlc_rel_df[gnb_iprlc_rel_df['gtp.out.sn'] == sn]
        gnb_rlc_rows = []
        for i in range(filtered_df.shape[0]):
            sdu_id = int(filtered_df.iloc[i]['sdu_id'])
            gnb_rlc_rows.append(gnb_rlc_segments_df[gnb_rlc_segments_df['sdu_id'] == sdu_id].iloc[0])

        filtered_df = ue_iprlc_rel_df[ue_iprlc_rel_df['rlc.txpdu.srn'] == sn]
        ip_id = int(filtered_df.iloc[0]['ip_id'])
        ue_ip_row = ue_ip_packets_df[ue_ip_packets_df['ip_id'] == ip_id].iloc[0]

        ue_rlc_rows = []
        for i in range(filtered_df.shape[0]):
            txpdu_id = filtered_df.iloc[i]['txpdu_id']
            ue_rlc_rows.append(ue_rlc_segments_df[ue_rlc_segments_df['txpdu_id'] == txpdu_id].iloc[0])

        input_ip_ts = plot_tree(gnb_ip_row, gnb_rlc_rows, ue_ip_row, ue_rlc_rows, input_ip_ts, ax)

        end_ts = max(end_ts,gnb_ip_row['gtp.out.timestamp'])
        begin_ts = min(begin_ts,ue_ip_row['ip.in.timestamp'])

    return begin_ts,end_ts


def plot_packet_tree_from_ueipids(ue_ipid_list : list, ue_ip_packets_df, ue_iprlc_rel_df, ue_rlc_segments_df, ue_mac_attempts_df, gnb_ip_packets_df, gnb_iprlc_rel_df, gnb_rlc_segments_df, gnb_mac_attempts_df, ax, plot):
   
    # first sort the ipids based on the packets arrival time
    ids_ts_list = []
    for ip_id in ue_ipid_list:
        ue_ip_row = ue_ip_packets_df[ue_ip_packets_df['ip_id'] == ip_id].iloc[0]
        ids_ts_list.append({ 'id':ip_id, 'ts':ue_ip_row['ip.in.timestamp']})

    sorted_ids_ts_list = sorted(ids_ts_list, key=lambda x: x['ts'])
    sorted_ids_list = [ di['id'] for di in sorted_ids_ts_list ]

    # then do the actual work
    end_ts = 0
    begin_ts = np.inf
    input_ip_ts = 0
    for ind, ip_id in enumerate(sorted_ids_list):
        ue_ip_row = ue_ip_packets_df[ue_ip_packets_df['ip_id'] == ip_id].iloc[0]
        filtered_df = ue_iprlc_rel_df[ue_iprlc_rel_df['ip_id'] == ip_id]
        sn_set = set()
        txpdu_id_set = set()
        for i in range(filtered_df.shape[0]):
            sn_set.add(filtered_df.iloc[i]['rlc.txpdu.srn'])
            txpdu_id_set.add(filtered_df.iloc[i]['txpdu_id'])

        logger.info(f"Found {len(sn_set)} related SN(s) and {len(txpdu_id_set)} TXPDU(s) for UE ip_id:{ip_id}")

        if len(sn_set) > 1:
            logger.error(f"More than one related ue SNs.")

        if len(sn_set) == 0:
            logger.error(f"No related ue SNs.")

        if len(txpdu_id_set) == 0:
            logger.error(f"No related ue TXPDU ids.")

        sn = sn_set.pop()
        logger.info(f"The UE SN found: {sn}")

        ue_rlc_rows = []
        for txpdu_id in txpdu_id_set:
            ue_rlc_rows.append(ue_rlc_segments_df[ue_rlc_segments_df['txpdu_id'] == txpdu_id].iloc[0])

        gnb_ip_row = gnb_ip_packets_df[gnb_ip_packets_df['gtp.out.sn'] == sn].iloc[0]
        filtered_df = gnb_iprlc_rel_df[gnb_iprlc_rel_df['gtp.out.sn'] == sn]
        gnb_rlc_rows = []
        for i in range(filtered_df.shape[0]):
            sdu_id = int(filtered_df.iloc[i]['sdu_id'])
            gnb_rlc_rows.append(gnb_rlc_segments_df[gnb_rlc_segments_df['sdu_id'] == sdu_id].iloc[0])

        logger.info(f"Found {len(txpdu_id_set)} gnb sdu_id(s) for SN:{sn}")

        if len(txpdu_id_set) == 0 :
            logger.error(f"No related gnb txpdu ids found.")

        if ind == 0:
            input_ip_ts = plot_packet_tree(gnb_ip_row, gnb_rlc_rows, gnb_rlc_segments_df, gnb_mac_attempts_df, ue_ip_row, ue_rlc_rows, ue_mac_attempts_df, input_ip_ts, ax, plot)
        else:
            plot_packet_tree(gnb_ip_row, gnb_rlc_rows, gnb_rlc_segments_df, gnb_mac_attempts_df, ue_ip_row, ue_rlc_rows, ue_mac_attempts_df, input_ip_ts, ax, plot)

        end_ts = max(end_ts,gnb_ip_row['gtp.out.timestamp'])
        begin_ts = min(begin_ts,ue_ip_row['ip.in.timestamp'])

    return begin_ts,end_ts


# def plot_packet_tree(packet, prev_input_ip_ts, ax):
#     # Starting point
#     x_start, y_start = 0, 0

#     # plot
#     if prev_input_ip_ts == 0:
#         x_os = 0
#     else:
#         x_os = (packet['ip.in_t'] - prev_input_ip_ts)*1000
#     x_start = x_start + x_os

#     branch0_x = x_start + (packet['rlc.in_t'] - packet['ip.in_t'])*1000
#     branch0_y = y_start + 1
#     ax.plot([x_start, branch0_x], [y_start, branch0_y], color='green')

#     # Iterate over each attempt
#     num_rlc_attempts = len(packet['rlc.attempts'])
#     for i in range(num_rlc_attempts):
#         tx_pdu_timestamp = packet['rlc.attempts'][i]['mac.in_t']
#         branch1_x = branch0_x + (tx_pdu_timestamp - packet['rlc.in_t'])*1000
#         branch1_y = branch0_y + 1

#         if not packet['rlc.attempts'][i]['acked']:
#           # this rlc segment could be nacked, check it TODO
#           # plot the rlc segment line
#           ax.plot([branch0_x, branch1_x], [branch0_y, branch1_y], color='red')
#           continue 
#         else:
#           # plot the rlc segment line
#           ax.plot([branch0_x, branch1_x], [branch0_y, branch1_y], color='blue')

#         for j in range(len(packet['rlc.attempts'][i]['mac.attempts'])):
#             if packet['rlc.attempts'][i]['mac.attempts'][j]['acked']:
#                 # ue side mac
#                 branch2_x = branch1_x + (
#                         packet['rlc.attempts'][i]['mac.attempts'][j]['phy.in_t']-tx_pdu_timestamp
#                     )*1000
#                 branch2_y = branch1_y + 1
#                 ax.plot([branch1_x, branch2_x], [branch1_y, branch2_y], color='orange')

#                 # gnb/ue side phy
#                 branch3_x = branch2_x + (
#                     packet['rlc.attempts'][i]['mac.attempts'][j]['phy.out_t'] - packet['rlc.attempts'][i]['mac.attempts'][j]['phy.in_t']
#                 )*1000
#                 branch3_y = branch2_y + 1
#                 ax.plot([branch2_x, branch3_x], [branch2_y, branch3_y], color='orange')

#                 # gnb side mac
#                 branch4_x = branch3_x + (
#                         packet['rlc.attempts'][i]['mac.out_t']-packet['rlc.attempts'][i]['mac.attempts'][j]['phy.out_t']
#                     )*1000
#                 branch4_y = branch3_y + 1
#                 ax.plot([branch3_x, branch4_x], [branch3_y, branch4_y], color='orange') 

#             else:
#                 # only ue side, in grey
#                 branch2_x = branch1_x + (
#                         packet['rlc.attempts'][i]['mac.attempts'][j]['phy.in_t']-packet['rlc.attempts'][i]['mac.in_t']
#                     )*1000
#                 branch2_y = branch1_y + 1
#                 ax.plot([branch1_x, branch2_x], [branch1_y, branch2_y], color='red') 

#     branch5_x = branch4_x + (packet['ip.out_t']-packet['rlc.out_t'])*1000
#     branch5_y = branch4_y + 1
#     ax.plot([branch4_x, branch5_x], [branch4_y, branch5_y], color='green')   

#     return packet['ip.in_t']

            
def ccdf(data):
    sorted_data = np.sort(data)
    ccdf_values = 1. - np.arange(1, len(data) + 1) / len(data)
    return sorted_data, ccdf_values

def plot_ccdfs(latencies, xlim=[5,12], ylim=[0.0001, 1], markers=[], linestyles=[], colors=[], filename='fig.png'):
    plt.figure(figsize=(12, 6))
    plt.ticklabel_format(style='plain', axis='x')
    index = 0
    for latency, label in latencies:
        sorted_latencies = np.sort(latency)
        ccdf = 1. - np.arange(1, len(sorted_latencies) + 1) / float(len(sorted_latencies))
        # Use the next marker from the list, cycling back if necessary
        marker = markers[index % len(markers)] if markers else 'o'
        linestyle = linestyles[index % len(linestyles)] if linestyles else 'solid'
        color = colors[index % len(colors)] if colors else 'b'
        index += 1
        plt.plot(sorted_latencies, ccdf, marker=marker, linestyle=linestyle,color=color, markersize=4, label=label)
    plt.title('CCDF of Latencies')
    plt.xlabel('Latency (ms)')
    plt.ylabel('CCDF')
    plt.grid(True, which="both", ls="--")
    plt.xlim(xlim)
    plt.ylim(ylim)
    plt.yscale("log")
    plt.legend()
    plt.savefig(filename) 
    plt.show()

def plot_ccdf(delays, label, figsize=(10, 6), outlier=35, x_lim=50):
    """
    Plots the Complementary Cumulative Distribution Function (CCDF) of the given delays.
    
    Parameters:
        delays (list or array-like): The delay values to plot.
        label (str): The label for the plot.
        figsize (tuple): The size of the figure (default is (10, 6)).
        
    Returns:
        fig, ax: The figure and axis objects of the plot.
    """
    fig, ax = plt.subplots(figsize=figsize)
    delays = delays[delays<outlier]
    # Sort the delay values and calculate the CCDF
    sorted_delays = np.sort(delays)
    ccdf = 1.0 - np.arange(1, len(sorted_delays) + 1) / len(sorted_delays)
    max_delay = sorted_delays[-1]

    # Plot the CCDF
    ax.plot(sorted_delays, ccdf, linestyle='-', linewidth=4, label=label)
    
    # Set the y-axis to a logarithmic scale
    ax.set_yscale('log')
    
    # Label the axes
    ax.set_xlabel('Delay (ms)', fontsize=15)
    ax.set_ylabel('Probability', fontsize=15)
    ax.tick_params(axis='both', labelsize=16)
    ax.set_xlim([-0.1, min(x_lim, max_delay+5)])

    
    # Add grid and legend
    ax.grid(True)
    ax.legend()
    
    return fig, ax

# Function to plot multiple CCDFs on the same axes
def plot_multiple_ccdf(data_dict, ax):
    for label, data in data_dict.items():
        sorted_data = np.sort(data)
        ccdf = 1. - np.arange(1, len(sorted_data) + 1) / len(sorted_data)
        ax.plot(sorted_data, ccdf, label=label)
    
    ax.set_xlabel('Delay (ms)')
    ax.set_ylabel('CCDF')
    ax.grid(True)
    ax.legend()

# Function to plot CCDFs from multiple meas files on the same axes
def plot_multiple_ccdf_per_delay_type(delay_values_per_meas, ax, delay_type_label='_', labels=[], outlier=35, x_lim=60):
    max_delay = -np.inf

    for idx, data in enumerate(delay_values_per_meas):
        delays = data[data<outlier]
        sorted_delays = np.sort(delays)
        ccdf = 1. - np.arange(1, len(sorted_delays) + 1) / len(sorted_delays)
        ax.plot(sorted_delays, ccdf, label=labels[idx])
        max_delay = max(max_delay, sorted_delays[-1])
    
    # Set the y-axis to a logarithmic scale
    ax.set_yscale('log')
    ax.set_xlim([-0.1, min(x_lim, max_delay)])
    ax.set_xlabel('Delay (ms)')
    ax.set_ylabel('CCDF')
    ax.grid(True)
    ax.legend()
    ax.set_title(f'CCDF of {delay_type_label}')


def plot_multiple_histograms(values_per_meas, ax, labels=[], y_log=True, delay_type_label='_', outlier=None):
    """
    Plots histograms for multiple arrays side by side on the same axes with normalized frequency.

    :param values_per_meas: List of arrays to plot
    :param labels: List of labels for each array
    :param ax: Axes object to plot on
    :param y_log: Boolean to set y-axis to log scale if True
    :param outlier: Cap value for outliers (optional)
    """
    # Cap values at the outlier threshold if specified
    if outlier is not None:
        values_per_meas = [np.minimum(data, outlier) for data in values_per_meas]
    
    # Determine bin edges based on the range across all arrays, with bins as max - min + 1
    min_value = int(min(np.min(data) for data in values_per_meas))
    max_value = int(max(np.max(data) for data in values_per_meas))
    bins = np.arange(min_value, max_value + 2)  # +2 to include max_value in bin centers
    bin_width = 1  # Each bin will represent an integer value
    width_total = 0.50
    # Plot each array as a separate bar chart, offset by `width`
    for idx, (data, label) in enumerate(zip(values_per_meas, labels)):
        hist, _ = np.histogram(data, bins=bins)
        hist = hist / len(data)  # Normalize frequency by the length of the data
        bin_centers = bins[:-1]  # Use each integer as the bin center

        # Offset each bar by its index for side-by-side effect, with width adjusted for clarity
        width = width_total*bin_width / (len(values_per_meas) + 0)  # Adjust width for multiple series
        offset = 0.5*(width)+(len(values_per_meas)-1)/2*width if len(values_per_meas)%2==0 else (len(values_per_meas)-1)/2*width
        ax.bar(bin_centers + idx * width - offset, 
                hist, width=width, alpha=0.6, label=label, edgecolor='black')
        # ax.bar(bin_centers + idx * width - (width * len(values_per_meas) / 4), 
        #        hist, width=width, alpha=0.6, label=label, edgecolor='black')
    
    ax.set_xlabel('Value')
    ax.set_ylabel('Normalized Frequency')
    ax.set_xticks(bins)
    if y_log:
        ax.set_yscale('log')
    ax.grid(True)
    ax.legend()
    ax.set_title(f'Histogram of {delay_type_label}')


def calculate_correlation(vector1, vector2):
    """
    Calculates and returns the Pearson correlation coefficient between two vectors.
    
    :param vector1: First vector (numpy array or list)
    :param vector2: Second vector (numpy array or list)
    :return: Correlation coefficient between vector1 and vector2
    """
    if len(vector1) != len(vector2):
        raise ValueError("Vectors must be the same length.")
    
    # Calculate correlation coefficient
    correlation_matrix = np.corrcoef(vector1, vector2)
    correlation_coefficient = correlation_matrix[0, 1]
    
    return correlation_coefficient

def plot_autocorrelation(data, max_lag=None):
        """
        Plots the autocorrelation of a given list of data for different lag values.

        Parameters:
        - data (list or np.array): Input list of numerical values.
        - max_lag (int, optional): Maximum lag to compute autocorrelation for. 
        Defaults to len(data) - 1 if not specified.
        """
        # Convert input data to a NumPy array for efficient computation
        data = np.array(data)

        if max_lag is None:
            max_lag = len(data) - 1

        # Initialize an array to store autocorrelation values
        autocorr_values = []

        # Compute autocorrelation for each lag
        for lag in range(max_lag + 1):
            # Lagged data
            y1 = data[:len(data) - lag]
            y2 = data[lag:]

            # Normalize to get the autocorrelation value
            corr = np.corrcoef(y1, y2)[0, 1] if len(y1) > 1 else 0
            autocorr_values.append(corr)
            # Plot the autocorrelation values
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.stem(range(max_lag + 1), autocorr_values, use_line_collection=True)
            ax.set_title('Autocorrelation Plot')
            ax.set_xlabel('Lag')
            ax.set_ylabel('Autocorrelation')
            ax.grid(True)
            return fig, ax
