#!/usr/bin/env python3
"""Conservative v0.1.1 loader for legacy hourly Dukascopy EUR/USD .bi5 files.

Decodes one or more hourly files, validates records, preserves wide spreads,
assigns UTC timestamps from the requested date + hour in each filename, audits
file boundaries and gaps, and writes clean ticks plus JSON/CSV audit outputs.

Assumptions for the legacy hourly format (must be revalidated for other formats):
  * LZMA-alone compressed payload
  * 20-byte big-endian records: uint32 millisecond offset, uint32 ask,
    uint32 bid, float32 ask volume, float32 bid volume
  * price scale 1e5; time offset from start of hour

This script does NOT generate signals or trade recommendations.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import lzma
import math
import re
import struct
import sys
from pathlib import Path
from collections import Counter

RECORD = struct.Struct('>IIIff')
RECORD_SIZE = RECORD.size
PRICE_SCALE = 100_000
PIP_SIZE_EURUSD = 0.0001
HOUR_MS = 3_600_000
HOUR_RE = re.compile(r'(?P<hour>\d{2})h_ticks\.bi5$', re.IGNORECASE)


def iso_utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_file(path: Path, date: dt.date, wide_spread_pips: float, gap_reset_seconds: float = 300.0):
    name_match = HOUR_RE.search(path.name)
    if not name_match:
        raise ValueError(f"Cannot infer hour from filename {path.name!r}; expected e.g. 13h_ticks.bi5")
    hour = int(name_match.group('hour'))
    if not 0 <= hour <= 23:
        raise ValueError(f"Invalid hour {hour} in {path.name}")
    file_dt = dt.datetime(date.year, date.month, date.day, hour, tzinfo=dt.timezone.utc)
    file_start_ms = int(file_dt.timestamp() * 1000)
    raw = path.read_bytes()
    result = {
        'source_file': path.name,
        'source_path': str(path),
        'requested_date_utc': date.isoformat(),
        'hour_utc': hour,
        'compressed_bytes': len(raw),
        'decompression_ok': False,
        'decompressed_bytes': None,
        'record_size_bytes': RECORD_SIZE,
        'record_count': 0,
        'records_remainder_bytes': None,
        'first_offset_ms': None,
        'last_offset_ms': None,
        'first_tick_utc': None,
        'last_tick_utc': None,
        'timestamp_decreases': 0,
        'same_millisecond_adjacent': 0,
        'exact_duplicate_records': 0,
        'invalid_price_or_quote': 0,
        'invalid_volume': 0,
        'offset_outside_hour': 0,
        'wide_spread_count': 0,
        'max_spread_pips': None,
        'median_spread_pips': None,
        'max_valid_tick_gap_seconds': None,
        'gaps_over_30_seconds': 0,
        'gaps_over_reset_threshold': 0,
        'min_mid': None,
        'max_mid': None,
        'status': 'not_processed',
    }
    # Some market-closed hours are legitimately represented by a zero-byte file.
    # Treat that as an empty hour, not a decompression failure.
    if len(raw) == 0:
        result['decompression_ok'] = True
        result['decompressed_bytes'] = 0
        result['records_remainder_bytes'] = 0
        result['status'] = 'empty_hour'
        result['note'] = 'Zero-byte file treated as a valid empty hour; no ticks emitted.'
        return result, []
    try:
        payload = lzma.decompress(raw, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError as e:
        result['status'] = 'decompression_error'
        result['error'] = str(e)
        return result, []
    result['decompression_ok'] = True
    result['decompressed_bytes'] = len(payload)
    result['records_remainder_bytes'] = len(payload) % RECORD_SIZE
    if len(payload) % RECORD_SIZE:
        result['status'] = 'record_size_error'
        result['error'] = f'Decompressed size {len(payload)} is not a multiple of {RECORD_SIZE}'
        return result, []
    records = []
    prev_offset = None
    prev_valid_offset = None
    valid_gaps_seconds = []
    seen_triplets = set()
    spreads = []
    mids = []
    for i in range(0, len(payload), RECORD_SIZE):
        offset, ask_i, bid_i, ask_vol, bid_vol = RECORD.unpack_from(payload, i)
        result['record_count'] += 1
        if prev_offset is not None:
            if offset < prev_offset:
                result['timestamp_decreases'] += 1
            elif offset == prev_offset:
                result['same_millisecond_adjacent'] += 1
        prev_offset = offset
        if offset >= HOUR_MS:
            result['offset_outside_hour'] += 1
        ask = ask_i / PRICE_SCALE
        bid = bid_i / PRICE_SCALE
        if ask_i <= 0 or bid_i <= 0 or ask_i <= bid_i:
            result['invalid_price_or_quote'] += 1
            continue
        if not math.isfinite(ask_vol) or not math.isfinite(bid_vol) or ask_vol < 0 or bid_vol < 0:
            result['invalid_volume'] += 1
            # Quote itself remains usable; volume is retained as decoded, but flagged in audit.
        ts_ms = file_start_ms + offset
        key = (offset, bid_i, ask_i)
        duplicate = key in seen_triplets
        if duplicate:
            result['exact_duplicate_records'] += 1
            continue
        seen_triplets.add(key)
        if prev_valid_offset is not None and offset >= prev_valid_offset:
            gap_seconds = (offset - prev_valid_offset) / 1000.0
            valid_gaps_seconds.append(gap_seconds)
            if gap_seconds > 30:
                result['gaps_over_30_seconds'] += 1
            if gap_seconds >= gap_reset_seconds:   # v0.1.1 (C3): >= and CLI value, matches segmentation
                result['gaps_over_reset_threshold'] += 1
        prev_valid_offset = offset
        spread_pips = (ask - bid) / PIP_SIZE_EURUSD
        mid = (ask + bid) / 2
        spreads.append(spread_pips)
        mids.append(mid)
        records.append({
            'timestamp_ms': ts_ms,
            'timestamp_utc': iso_utc(ts_ms),
            'timestamp_offset_ms': offset,
            'pair': 'EURUSD',
            'bid': f'{bid:.5f}',
            'ask': f'{ask:.5f}',
            'mid': f'{mid:.6f}',
            'spread': f'{ask-bid:.5f}',
            'spread_pips': f'{spread_pips:.2f}',
            'wide_spread_flag': str(spread_pips > wide_spread_pips).lower(),
            'ask_volume': f'{ask_vol:.8g}',
            'bid_volume': f'{bid_vol:.8g}',
            'source_file': path.name,
            'source_hour_utc': hour,
            'source_record_index': i // RECORD_SIZE,
            'segment_id': '',
            'detection_eligible': '',
        })
    if records:
        result['first_offset_ms'] = records[0]['timestamp_offset_ms']
        result['last_offset_ms'] = records[-1]['timestamp_offset_ms']
        result['first_tick_utc'] = records[0]['timestamp_utc']
        result['last_tick_utc'] = records[-1]['timestamp_utc']
        result['max_spread_pips'] = round(max(spreads), 4)
        sorted_spreads = sorted(spreads)
        n = len(sorted_spreads)
        result['median_spread_pips'] = round((sorted_spreads[(n-1)//2] + sorted_spreads[n//2]) / 2, 4)
        result['wide_spread_count'] = sum(s > wide_spread_pips for s in spreads)
        result['max_valid_tick_gap_seconds'] = round(max(valid_gaps_seconds), 3) if valid_gaps_seconds else None
        result['min_mid'] = round(min(mids), 5)
        result['max_mid'] = round(max(mids), 5)
    result['status'] = 'ok' if result['timestamp_decreases'] == 0 and result['offset_outside_hour'] == 0 else 'needs_review'
    if result['timestamp_decreases']:
        result['warning'] = 'Out-of-order records detected. Entire file is quarantined from clean output; review source file.'
        return result, []
    if result['offset_outside_hour']:
        result['warning'] = 'Timestamp offset outside hour detected. Entire file is quarantined from clean output; review source file.'
        return result, []
    return result, records


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--date', required=True, help='UTC calendar date for all input hourly files, YYYY-MM-DD')
    ap.add_argument('--out-dir', default='loader_output', help='Output directory (default: loader_output)')
    ap.add_argument('--wide-spread-pips', type=float, default=3.0,
                    help='Flag spreads strictly above this value; never drops them (default: 3.0 pips)')
    ap.add_argument('--gap-reset-seconds', type=float, default=300.0,
                    help='Start a new segment when consecutive valid ticks are at least this far apart (default: 300 s)')
    ap.add_argument('files', nargs='+', help='Input hourly .bi5 files, e.g. 03h_ticks.bi5 13h_ticks.bi5')
    args = ap.parse_args()
    try:
        date = dt.date.fromisoformat(args.date)
    except ValueError:
        ap.error('--date must be YYYY-MM-DD')
    if args.wide_spread_pips < 0 or args.gap_reset_seconds <= 0:
        ap.error('spread threshold must be >= 0 and gap reset must be > 0')

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_file_audits = []
    all_records = []
    for pstr in args.files:
        path = Path(pstr)
        audit, records = parse_file(path, date, args.wide_spread_pips, args.gap_reset_seconds)
        per_file_audits.append(audit)
        all_records.extend(records)

    # Sort by reconstructed UTC timestamp, then input-file order and source record index.
    # This preserves original file order for same-millisecond ticks within a file.
    file_order = {Path(p).name: i for i, p in enumerate(args.files)}
    all_records.sort(key=lambda r: (r['timestamp_ms'], file_order.get(r['source_file'], 999999), r['source_record_index']))

    # Quarantine timestamps that go backwards in source order was already flagged in file audit.
    # For merged output, retain equal timestamps (different quote values are not duplicates).
    # Drop only exact timestamp+bid+ask duplicates globally.
    merged = []
    seen_global = set()
    duplicate_global = 0
    for r in all_records:
        key = (r['timestamp_ms'], r['bid'], r['ask'])
        if key in seen_global:
            duplicate_global += 1
            continue
        seen_global.add(key)
        merged.append(r)

    # Segment on gaps >= configured threshold (v0.1.1). Warm-up lasts 300 seconds from segment's first tick.
    gap_rows = []
    segment_no = 0
    segment_start_ms = None
    previous = None
    for r in merged:
        ts = r['timestamp_ms']
        gap_seconds = None if previous is None else (ts - previous['timestamp_ms']) / 1000.0
        is_reset = previous is None or gap_seconds >= args.gap_reset_seconds   # v0.1.1 (C1): spec says >= 300 s resets
        if previous is not None and gap_seconds > 30:
            gap_rows.append({
                'previous_tick_utc': previous['timestamp_utc'],
                'next_tick_utc': r['timestamp_utc'],
                'gap_seconds': round(gap_seconds, 3),
                'new_segment_id': f'S{segment_no + 1:06d}' if is_reset else f'S{segment_no:06d}',
                'segment_reset': str(is_reset).lower(),
                'reason': 'elapsed_time_exceeds_gap_reset_threshold' if is_reset else 'long_gap_below_reset_threshold',
            })
        if is_reset:
            segment_no += 1
            segment_start_ms = ts
        r['segment_id'] = f'S{segment_no:06d}'
        r['detection_eligible'] = str(ts - segment_start_ms >= 300_000).lower()
        previous = r

    clean_path = out_dir / 'clean_ticks.csv'
    fields = ['timestamp_utc','timestamp_ms','timestamp_offset_ms','pair','bid','ask','mid','spread','spread_pips','wide_spread_flag','ask_volume','bid_volume','source_file','source_hour_utc','source_record_index','segment_id','detection_eligible']
    with clean_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in merged:
            w.writerow({k:r.get(k,'') for k in fields})

    gaps_path = out_dir / 'gap_audit.csv'
    with gaps_path.open('w', newline='', encoding='utf-8') as f:
        fields_gap = ['previous_tick_utc','next_tick_utc','gap_seconds','new_segment_id','segment_reset','reason']
        w = csv.DictWriter(f, fieldnames=fields_gap)
        w.writeheader(); w.writerows(gap_rows)

    # File-boundary continuity audit in requested-hour order.
    boundary_rows = []
    file_order_audits = sorted(per_file_audits, key=lambda a: (a.get('requested_date_utc') or '', a.get('hour_utc') if a.get('hour_utc') is not None else -1))
    for left, right in zip(file_order_audits, file_order_audits[1:]):
        if left.get('last_tick_utc') and right.get('first_tick_utc'):
            left_ms = int(dt.datetime.fromisoformat(left['last_tick_utc'].replace('Z', '+00:00')).timestamp() * 1000)
            right_ms = int(dt.datetime.fromisoformat(right['first_tick_utc'].replace('Z', '+00:00')).timestamp() * 1000)
            elapsed = (right_ms - left_ms) / 1000.0
            boundary_rows.append({
                'left_file': left['source_file'], 'left_last_tick_utc': left['last_tick_utc'],
                'right_file': right['source_file'], 'right_first_tick_utc': right['first_tick_utc'],
                'elapsed_seconds': round(elapsed, 3),
                'hours_are_adjacent': str(right.get('hour_utc') == left.get('hour_utc', -2) + 1).lower(),
                'continuous_under_gap_threshold': str(0 <= elapsed < args.gap_reset_seconds).lower(),   # v0.1.1 (C2)
                'note': 'Continuity check only; does not prove no ticks are missing.'
            })
    boundary_path = out_dir / 'file_boundary_audit.csv'
    with boundary_path.open('w', newline='', encoding='utf-8') as f:
        boundary_fields = ['left_file','left_last_tick_utc','right_file','right_first_tick_utc','elapsed_seconds','hours_are_adjacent','continuous_under_gap_threshold','note']
        w = csv.DictWriter(f, fieldnames=boundary_fields)
        w.writeheader(); w.writerows(boundary_rows)

    audit = {
        'loader_version': '0.1.1',
        'pair': 'EURUSD',
        'requested_date_utc': date.isoformat(),
        'format_assumptions': {
            'compression': 'LZMA auto-detected; actual files successfully decompressed',
            'record_size_bytes': RECORD_SIZE,
            'record_struct': '>IIIff (offset_ms, ask_int, bid_int, ask_volume_float32, bid_volume_float32)',
            'timestamp_basis': 'milliseconds from start of filename hour, combined with --date in UTC',
            'price_scale': PRICE_SCALE,
            'duplicate_definition': 'identical reconstructed timestamp + bid + ask; equal timestamps with different quotes are retained',
        },
        'configuration': {
            'wide_spread_flag_threshold_pips': args.wide_spread_pips,
            'wide_spreads_are_dropped': False,
            'gap_reset_seconds': args.gap_reset_seconds,
            'warmup_seconds_per_segment': 300,
            'window_lower_bound_convention': 'inclusive: timestamp >= t - 300 seconds',
        },
        'input_files': per_file_audits,
        'merge_summary': {
            'records_after_per_file_structural_quote_validation_and_exact_dedup': len(all_records),   # v0.1.1 (C4): quarantined files emit nothing
            'records_written_clean_ticks': len(merged),
            'exact_duplicates_dropped_across_merged_files': duplicate_global,
            'segments': segment_no,
            'gap_count': len(gap_rows),
            'detection_eligible_ticks': sum(r['detection_eligible'] == 'true' for r in merged),
            'detection_ineligible_warmup_ticks': sum(r['detection_eligible'] == 'false' for r in merged),
            'wide_spread_flagged_ticks': sum(r['wide_spread_flag'] == 'true' for r in merged),
            'clean_ticks_csv': clean_path.name,
            'gap_audit_csv': gaps_path.name,
            'file_boundary_audit_csv': boundary_path.name,
            'important_note': 'Any file with out-of-order timestamps or offsets outside the hour is quarantined entirely from clean output. Long gaps >30 seconds are listed in gap_audit.csv; gaps >= configured reset threshold start a new segment.'
        }
    }
    audit_path = out_dir / 'audit_report.json'
    audit_path.write_text(json.dumps(audit, indent=2), encoding='utf-8')
    per_file_path = out_dir / 'file_audit.csv'
    flat_fields = ['source_file','requested_date_utc','hour_utc','compressed_bytes','decompression_ok','decompressed_bytes','record_size_bytes','record_count','records_remainder_bytes','first_offset_ms','last_offset_ms','first_tick_utc','last_tick_utc','timestamp_decreases','same_millisecond_adjacent','exact_duplicate_records','invalid_price_or_quote','invalid_volume','offset_outside_hour','wide_spread_count','max_spread_pips','median_spread_pips','max_valid_tick_gap_seconds','gaps_over_30_seconds','gaps_over_reset_threshold','min_mid','max_mid','status','error','warning','note']
    with per_file_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=flat_fields, extrasaction='ignore')
        w.writeheader(); w.writerows(per_file_audits)

    print(json.dumps({
        'output_dir': str(out_dir.resolve()),
        'files': [str(clean_path), str(audit_path), str(per_file_path), str(gaps_path), str(boundary_path)],
        'input_files': [{k:a.get(k) for k in ['source_file','record_count','decompressed_bytes','timestamp_decreases','same_millisecond_adjacent','invalid_price_or_quote','wide_spread_count','max_spread_pips','max_valid_tick_gap_seconds','gaps_over_30_seconds','status']} for a in per_file_audits],
        'merge_summary': audit['merge_summary']
    }, indent=2))
    if any(a.get('status') != 'ok' for a in per_file_audits):
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
