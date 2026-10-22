import csv

import pytest

from receive import CSV_HEADER, needs_header


def write_header(path, header):
    with open(path, 'w', newline='') as f:
        csv.writer(f).writerow(header)


def test_new_or_empty_file_needs_header(tmp_path):
    path = tmp_path / 'stream.csv'
    assert needs_header(str(path), fresh=False)
    path.write_text('')
    assert needs_header(str(path), fresh=False)


def test_matching_file_is_appended(tmp_path):
    path = tmp_path / 'stream.csv'
    write_header(path, CSV_HEADER)
    assert not needs_header(str(path), fresh=False)


def test_fresh_always_rewrites(tmp_path):
    path = tmp_path / 'stream.csv'
    write_header(path, CSV_HEADER[:6] + ['Int_0'])
    assert needs_header(str(path), fresh=True)


def test_mismatched_sample_count_refuses_to_append(tmp_path):
    path = tmp_path / 'stream.csv'
    write_header(path, CSV_HEADER[:6] + [f'Int_{i}' for i in range(2048)])
    with pytest.raises(ValueError, match='2048 samples/row'):
        needs_header(str(path), fresh=False)
