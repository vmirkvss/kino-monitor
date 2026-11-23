import copy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import yaml
from main import Kino, MonitorError, matches, new_state, parse_session, process, read_state, save_state, validate_schedule
CFG = yaml.safe_load(Path('config.yaml').read_text())
DAY = (datetime.now() + timedelta(days=200)).strftime('%Y-%m-%d')
MOVIE = {'id': 12344, 'name': 'Мстители: Доктор Дум', 'name_origin': 'Avengers: Doomsday'}
ROW = {'session': {'id': 999, 'session_id': 123, 'cinema_id': 837, 'movie_id': 12344, 'session_type': 1, 'session_date': DAY + 'T00:00:00Z', 'hour': '10', 'minutes': '20'}, 'cinema': {'id': 837, 'name': 'Chaplin MEGA Alma-Ata (ул. Розыбакиева)', 'latitude': 43.2, 'longitude': 76.9}, 'hall': {'name': 'Зал 1'}}

class MonitorTests(unittest.TestCase):
    def test_film_aliases(self):
        for title in ['AVENGERS: DOOMSDAY', 'Мстители: Судный день', 'Мстители: Доктор Дум']:
            self.assertTrue(matches(title, CFG['movie']['keyword_groups']))
        self.assertFalse(matches('Мстители: Финал', CFG['movie']['keyword_groups']))

    def test_cinema_filter(self):
        for name, accepted in [('Chaplin MEGA Alma-Ata', True), ('Cinemax Dostyk Multiplex', True), ('Chaplin MEGA Park', False), ('Chaplin ADK', False), ('Арман 3D (пр. Достык)', False), ('Kinopark 11', False)]:
            row = copy.deepcopy(ROW)
            row['cinema']['name'] = name
            self.assertEqual(parse_session(row, MOVIE, CFG) is not None, accepted, name)

    def test_city_and_sale_type(self):
        row = copy.deepcopy(ROW)
        row['cinema']['latitude'] = 51.1
        self.assertIsNone(parse_session(row, MOVIE, CFG))
        row = copy.deepcopy(ROW)
        row['session']['session_type'] = 2
        self.assertIsNone(parse_session(row, MOVIE, CFG))

    def test_public_id_local_time(self):
        item = parse_session(ROW, MOVIE, CFG)
        self.assertIn('/tickets/123?', item['url'])
        self.assertTrue(item['when'].endswith('10:20'))

    def test_structure_change(self):
        with self.assertRaises(MonitorError): validate_schedule({'data': []})
        with self.assertRaises(MonitorError): parse_session({'session': {}, 'cinema': {'name': 'Unknown'}}, MOVIE, CFG)
        self.assertEqual(validate_schedule({'sessions': None, 'available_dates': []}), ([], []))

    def test_all_dates(self):
        kino = Kino(CFG)
        day2 = (datetime.now() + timedelta(days=201)).strftime('%Y-%m-%d')
        kino.query = Mock(side_effect=[[MOVIE], {'sessions': [], 'available_dates': [DAY, day2]}, {'sessions': [ROW], 'available_dates': [DAY, day2]}, {'sessions': [ROW], 'available_dates': [DAY, day2]}])
        self.assertEqual(len(kino.collect()), 1)
        self.assertEqual(kino.query.call_count, 4)

    def test_dedup_and_new(self):
        state, send = new_state(), Mock()
        collect = Mock(return_value=[parse_session(ROW, MOVIE, CFG)])
        process(state, CFG, collect, send, lambda: None, 100000)
        self.assertEqual(send.call_count, 2)
        process(state, CFG, collect, send, lambda: None, 100300)
        self.assertEqual(send.call_count, 2)
        row = copy.deepcopy(ROW)
        row['session']['session_id'] = 124
        collect.return_value.append(parse_session(row, MOVIE, CFG))
        process(state, CFG, collect, send, lambda: None, 100600)
        self.assertEqual(send.call_count, 3)

    def test_send_failure(self):
        state = new_state()
        with self.assertRaises(MonitorError):
            process(state, CFG, lambda: [parse_session(ROW, MOVIE, CFG)], Mock(side_effect=MonitorError('Telegram down')), lambda: None, 100000)
        self.assertEqual(state['seen'], {})

    def test_warning_and_recovery(self):
        state, send = new_state(), Mock()
        fail = Mock(side_effect=MonitorError('API down'))
        for n in range(4): process(state, CFG, fail, send, lambda: None, 100000+n)
        self.assertEqual(send.call_count, 1)
        process(state, CFG, lambda: [], send, lambda: None, 100010)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(state['failures'], 0)
        self.assertFalse(state['warned'])

    def test_daily_heartbeat(self):
        state, send = new_state(), Mock()
        for now in [100000, 100300, 186400]: process(state, CFG, lambda: [], send, lambda: None, now)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(state['failures'], 0)

    def test_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = new_state()
            state['seen']['abc'] = 100
            save_state(path, state)
            self.assertEqual(read_state(path), state)
            path.write_text('{}')
            with self.assertRaises(MonitorError): read_state(path)

if __name__ == '__main__': unittest.main()
