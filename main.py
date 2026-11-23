#!/usr/bin/env python3
"""Read-only kino.kz monitor. One invocation = one polling cycle (GitHub Actions)."""
import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import sys
import time
from zoneinfo import ZoneInfo

import requests
import yaml

LOG = logging.getLogger("kino-monitor")


class MonitorError(Exception):
    pass


def matches(text, groups):
    text = text.casefold()
    return all(any(word.casefold() in text for word in group) for group in groups)


def new_state():
    return {"version": 1, "seen": {}, "failures": 0, "warned": False,
            "last_heartbeat": 0, "next_poll": 0}


def read_state(path):
    if not path.exists():
        return new_state()
    try:
        state = json.loads(path.read_text())
        if (state['version'] != 1 or not isinstance(state['seen'], dict)
                or not isinstance(state['failures'], int)
                or not isinstance(state['warned'], bool)
                or not isinstance(state['last_heartbeat'], (int, float))
                or not isinstance(state['next_poll'], (int, float))):
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError):
        # Never silently erase deduplication history.
        raise MonitorError("state.json повреждён; восстановите файл из ветки monitor-state") from None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


class Telegram:
    def __init__(self, timeout=25):
        token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        if not token or not self.chat_id:
            raise MonitorError('Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в Secrets')
        self.url = f'https://api.telegram.org/bot{token}/sendMessage'
        self.timeout = timeout

    def send(self, text):
        # Do not blindly retry POST after timeout: Telegram may already have sent it.
        try:
            response = requests.post(self.url, json={
                'chat_id': self.chat_id, 'text': text,
                'link_preview_options': {'is_disabled': True},
            }, timeout=(10, self.timeout))
        except requests.exceptions.ConnectTimeout:
            raise MonitorError('Telegram: ConnectTimeout — соединение не установлено за 10 секунд') from None
        except requests.exceptions.ReadTimeout:
            raise MonitorError(
                f'Telegram: ReadTimeout — ответ не получен за {self.timeout} секунд; '
                'сообщение могло быть доставлено, проверьте чат') from None
        except requests.exceptions.SSLError:
            raise MonitorError('Telegram: SSLError — ошибка проверки TLS-сертификата') from None
        except requests.exceptions.ConnectionError:
            raise MonitorError('Telegram: ConnectionError — ошибка DNS, подключения или разрыв соединения') from None
        except requests.RequestException:
            # Never log exception text: it may contain the bot token in the URL.
            raise MonitorError('Telegram: RequestException — ошибка HTTP-клиента') from None
        try:
            data = response.json()
        except ValueError:
            raise MonitorError(
                f'Telegram: HTTP {response.status_code}, ответ не является JSON') from None
        if not isinstance(data, dict):
            raise MonitorError(f'Telegram: HTTP {response.status_code}, неизвестный формат ответа')
        if response.status_code != 200 or data.get('ok') is not True:
            code = data.get('error_code', response.status_code)
            hints = {
                400: 'проверьте chat_id и нажмите Start у своего бота',
                401: 'токен не принят; проверьте TELEGRAM_BOT_TOKEN',
                403: 'боту запрещено писать в чат; проверьте, не заблокирован ли он',
                404: 'проверьте формат токена в TELEGRAM_BOT_TOKEN',
            }
            hint = hints.get(code, 'Telegram отклонил запрос') if isinstance(code, int) else 'неизвестный код ошибки'
            if code == 429:
                params = data.get('parameters')
                delay = params.get('retry_after') if isinstance(params, dict) else None
                hint = (f'лимит запросов; повторите не раньше чем через {delay} секунд'
                        if isinstance(delay, int) and delay >= 0 else 'лимит запросов')
            raise MonitorError(f'Telegram: HTTP {response.status_code}; {hint}')



class Kino:
    def __init__(self, config):
        self.cfg = config
        self.http = requests.Session()
        self.http.headers.update({'User-Agent': config['user_agent'],
                                  'Accept': 'application/json', 'Accept-Language': 'ru',
                                  'Referer': config['site'] + '/ru/'})
        self.http.cookies.set(config['city_cookie'], str(config['city_id']))
        self.last_request = 0.0

    def query(self, procedure, payload):
        for attempt in range(self.cfg['retries'] + 1):
            time.sleep(max(0, self.cfg['request_pause_seconds'] -
                           (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                r = self.http.get(self.cfg['site'] + '/api/trpc/' + procedure,
                                  params={'input': json.dumps({'json': payload})},
                                  timeout=(10, self.cfg['timeout_seconds']))
                if r.status_code == 429:
                    # Respect rate limiting; no immediate retries in this cycle.
                    raise MonitorError('kino.kz ограничил запросы (429); повтор в следующем цикле')
                if r.status_code >= 500:
                    raise requests.ConnectionError('server error')
                if r.status_code != 200:
                    raise MonitorError(f'kino.kz: HTTP {r.status_code}, {procedure}')
                return r.json()['result']['data']['json']
            except (KeyError, TypeError, ValueError):
                raise MonitorError(f'Изменилась JSON-структура {procedure}') from None
            except requests.RequestException:
                if attempt == self.cfg['retries']:
                    raise MonitorError(f'kino.kz недоступен: {procedure}') from None
                time.sleep(2 ** (attempt + 1) + random.random())

    def collect(self):
        cfg = self.cfg
        today = datetime.now(ZoneInfo(cfg['timezone'])).date()
        premiere = date.fromisoformat(cfg['movie']['premiere'])
        end = max(today + timedelta(days=cfg['movie']['lookahead_days']), premiere)
        movies = self.query('sessions.findMovies', {'startDate': str(today), 'endDate': str(end)})
        if not isinstance(movies, list) or not movies:
            raise MonitorError('Каталог фильмов пуст или структура изменилась')
        found = []
        for movie in movies:
            if not isinstance(movie, dict) or 'id' not in movie or 'name' not in movie:
                raise MonitorError('Неизвестная структура записи фильма')
            names = ' '.join(str(movie.get(k) or '') for k in ('name', 'name_rus', 'name_origin'))
            if matches(names, cfg['movie']['keyword_groups']):
                found.append(movie)
        if not found:
            raise MonitorError('Фильм не найден в каталоге; возможно, изменилось название или расписание')
        results, total_sessions = {}, 0
        for movie in found:
            first_date = str(max(today, premiere))
            pending, done = [first_date], set()
            while pending:
                day = pending.pop(0)
                if day in done:
                    continue
                done.add(day)
                if len(done) > cfg['max_schedule_dates']:
                    raise MonitorError('Слишком много дат расписания; увеличьте max_schedule_dates')
                data = self.query('sessions.getSessions', {'id': movie['id'], 'date': day})
                rows, dates = validate_schedule(data)
                pending.extend(d for d in dates if d >= str(today) and d not in done and d not in pending)
                total_sessions += len(rows)
                for row in rows:
                    item = parse_session(row, movie, cfg)
                    if item:
                        results[item['key']] = item
        if not total_sessions:
            raise MonitorError('Расписание фильма пусто во всех доступных датах; продажи могли быть сняты')
        return sorted(results.values(), key=lambda item: (item['when'], item['cinema']))


def validate_schedule(data):
    if not isinstance(data, dict) or not {'sessions', 'available_dates'} <= data.keys():
        raise MonitorError('В расписании отсутствуют sessions/available_dates')
    rows, dates = data['sessions'], data['available_dates']
    # The website explicitly handles null sessions on dates with no showtimes.
    rows = [] if rows is None else rows
    if not isinstance(rows, list) or not isinstance(dates, list):
        raise MonitorError('Неверный тип списка сеансов/дат')
    for day in dates:
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except (ValueError, TypeError):
            raise MonitorError('Неизвестный формат даты расписания') from None
    return rows, dates


def parse_session(row, movie, cfg):
    try:
        s, cinema = row['session'], row['cinema']
        name = cinema['name']
        # Validate every record, not only matching cinemas, to detect schema drift.
        required = ('id', 'session_id', 'cinema_id', 'movie_id', 'session_type',
                    'session_date', 'hour', 'minutes')
        if any(k not in s for k in required) or not isinstance(name, str):
            raise ValueError()
        if s['movie_id'] != movie['id'] or s['cinema_id'] != cinema['id']:
            raise ValueError()
        day = date.fromisoformat(s['session_date'][:10])
        when = datetime.combine(day, datetime.min.time()).replace(
            hour=int(s['hour']), minute=int(s['minutes']), tzinfo=ZoneInfo(cfg['timezone']))
        target = any(matches(name, rule['keyword_groups']) and
                     not any(w.casefold() in name.casefold() for w in rule['exclude'])
                     for rule in cfg['cinemas'])
        if not target:
            return None
        lat, lon = float(cinema['latitude']), float(cinema['longitude'])
        a, b, c, d = cfg['city_bounds']
        if not (a <= lat <= b and c <= lon <= d):
            return None
        # Site uses session_type == 1 for clickable sale sessions.
        if s['session_type'] != 1 or when <= datetime.now(ZoneInfo(cfg['timezone'])):
            return None
        # Public purchase route uses session_id, NOT the internal record id.
        url = (f"{cfg['site']}/ru/movie/{movie['id']}/tickets/{s['session_id']}"
               f"?cityId={cfg['city_id']}&cinemaId={cinema['id']}")
        identity = f"kino.kz:{movie['id']}:{cinema['id']}:{s['session_id']}:{when.isoformat()}"
        return {'key': hashlib.sha256(identity.encode()).hexdigest(), 'cinema': name,
                'when': when.strftime('%d.%m.%Y %H:%M'), 'url': url,
                'movie': movie['name'], 'hall': row.get('hall', {}).get('name', ''),
                'language': s.get('lang_label', '')}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MonitorError('Изменилась структура записи сеанса') from None


def process(state, cfg, collect, send, persist, now):
    """Persist each successful delivery; failed sends remain pending for next run."""
    try:
        items = collect()
    except MonitorError as exc:
        state['failures'] += 1
        persist()
        LOG.error('%s (сбой %d)', exc, state['failures'])
        if state['failures'] >= cfg['warning_threshold'] and not state['warned']:
            send(f'⚠️ kino.kz: {state["failures"]} неудачные проверки подряд.\n{exc}\n'
                 'Отсутствие билетов сейчас не подтверждено. Проверьте журнал GitHub Actions.')
            state['warned'] = True
            persist()
        return 1
    if state['warned']:
        send('✅ kino.kz снова отвечает, мониторинг восстановлен.')
    state['failures'], state['warned'] = 0, False
    persist()
    for item in items:
        if item['key'] in state['seen']:
            continue
        send(f"🎬 Новый сеанс — kino.kz\n{item['movie']}\n{item['cinema']}\n"
             f"{item['when']} (Алматы) · {item['hall']} · {item['language']}\n"
             f"{item['url']}")
        state['seen'][item['key']] = now
        persist()
    if now - state['last_heartbeat'] >= cfg['heartbeat_seconds']:
        if items:
            text = f'Работаю. Сейчас найдено сеансов в выбранных кинотеатрах: {len(items)}.'
        elif state['seen']:
            text = 'Работаю. Сейчас доступных сеансов нет; ранее найденные уже отправлял.'
        else:
            text = 'Работаю, билетов пока нет — kino.kz, Chaplin MEGA Alma-Ata и Cinemax Dostyk.'
        send(text)
        state['last_heartbeat'] = now
        persist()
    LOG.info('Проверка завершена; подходящих сеансов: %d', len(items))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--state', default='state.json')
    parser.add_argument('--test', action='store_true', help='Отправить тест в Telegram, не опрашивая сайт')
    parser.add_argument('--dry-run', action='store_true', help='Проверить API без Telegram и изменения состояния')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = yaml.safe_load(Path(args.config).read_text())
    if args.test:
        Telegram(cfg['timeout_seconds']).send('✅ Тест: монитор kino.kz подключён к Telegram.')
        return 0
    kino = Kino(cfg)
    if args.dry_run:
        print(json.dumps(kino.collect(), ensure_ascii=False, indent=2))
        return 0
    tg = Telegram(cfg['timeout_seconds'])
    path = Path(args.state)
    state = read_state(path)
    # Previous cycle chooses the next eligible instant: 300 ± 30 seconds.
    # Skip clustered delayed Actions jobs instead of hitting the site repeatedly.
    remaining = state['next_poll'] - time.time()
    if remaining > 60:
        LOG.info('Рано для следующего опроса; этот запуск пропущен')
        return 0
    if remaining > 0:
        time.sleep(remaining)
    elif not state['next_poll']:
        time.sleep(random.uniform(0, cfg['jitter_seconds']))
    now = time.time()
    state['next_poll'] = now + cfg['interval_seconds'] + random.uniform(
        -cfg['jitter_seconds'], cfg['jitter_seconds'])
    persist = lambda: save_state(path, state)
    persist()
    return process(state, cfg, kino.collect, tg.send, persist, now)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except MonitorError as exc:
        LOG.error('%s', exc)
        sys.exit(1)
