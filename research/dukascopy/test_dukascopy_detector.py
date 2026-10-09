"""Small deterministic tests for detector v0.1; uses only the standard library."""
import datetime as dt
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name('dukascopy_detector_v0_1.py')
spec = importlib.util.spec_from_file_location('detector', SCRIPT)
detector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detector)

def tick(ms, mid, eligible=True, segment='S000001', index=0):
    stamp = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return {
        'timestamp_ms': ms, 'mid_num': mid, 'timestamp_utc': stamp,
        'segment_id': segment, 'eligible': eligible, 'pair': 'EURUSD',
        'source_file': 'synthetic.bi5', 'source_record_index': str(index),
        'bid': f'{mid-0.00001:.5f}', 'ask': f'{mid+0.00001:.5f}',
        'spread_pips': '0.2', 'wide_spread_flag': 'false'
    }

def test_threshold_crossing_and_direction():
    b=1.10000
    rows=[tick(0,b,False, index=0),tick(100000,b,False,index=1),tick(200000,b,False,index=2),tick(300000,b,True,index=3),tick(400000,b-.00100,True,index=4),tick(450000,b+.00150,True,index=5)]
    e=detector.detect(rows,[25])
    assert len(e)==1 and e[0]['direction']=='UP' and e[0]['threshold_pips']==25

def test_no_repeat_and_natural_rearm():
    b=1.10000
    rows=[tick(0,b,False,index=0),tick(300000,b,True,index=1),tick(310000,b+.003,True,index=2),tick(320000,b+.0031,True,index=3),tick(620001,b+.0002,True,index=4),tick(630000,b+.0032,True,index=5)]
    e=detector.detect(rows,[25])
    assert len(e)==2, e

def test_segment_isolation():
    b=1.10000
    rows=[tick(0,b,False,segment='A',index=0),tick(300000,b,True,segment='A',index=1),tick(301000,b+.003,True,segment='B',index=2)]
    assert detector.detect(rows,[25]) == []

if __name__ == '__main__':
    test_threshold_crossing_and_direction()
    test_no_repeat_and_natural_rearm()
    test_segment_isolation()
    print('PASS: 3 detector tests (threshold crossing/direction; no repeated event and natural re-arm; segment isolation)')
