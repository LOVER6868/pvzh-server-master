"""Casual matchmaking, keyed by the ACTUAL card_data content fingerprint.

Existing challenge routes are preserved for mod-compatible clients. The
content gate runs before any queue operation and no device approval is used.
"""
import json
import secrets
import time

from flask import Blueprint, g, request
from utils import get_id, it_should_be_there_soon, r
from content_gate import verify_content_header

matchmaking = Blueprint('matchmaking', __name__, url_prefix='/matchmaking/v1')
WAIT_TTL = 180
MATCH_TTL = 900
WAIT_ETA = 5
MAX_QUEUE = 200


def txt(v):
    return v.decode('utf-8') if isinstance(v, bytes) else v


def player_key(pid):
    return f'contentmatch:player:{pid}'


def queue_key(content, faction):
    return f'contentmatch:queue:{content}:{faction}'


def state(pid):
    if not pid:
        return {}
    return {txt(k): txt(v) for k, v in r.hgetall(player_key(pid)).items()}


def fail(message, status=400):
    return {'ty': 'MatchFailed', 'error': message}, status


def waiting():
    return {'ty': 'WaitingForMatch', 'eta': WAIT_ETA, 'gi': None}


def paired(opponent):
    return {'ty': 'JoinMatch', 'eta': 0, 'gi': opponent}


def selection(body):
    raw = body.get('sd')
    if isinstance(raw, dict):
        parsed = raw
        sd = json.dumps(raw, separators=(',', ':'))
    elif isinstance(raw, str):
        parsed = json.loads(raw)
        sd = raw
    else:
        raise ValueError('missing selection data sd')
    if not isinstance(parsed, dict):
        raise ValueError('selection data must be an object')
    pid = str(parsed['PlayerId'])
    faction = str(parsed['Faction']).lower()
    if faction.startswith('plant'):
        faction = 'Plants'
    elif faction.startswith('zomb'):
        faction = 'Zombies'
    else:
        raise ValueError('unknown faction')
    if not pid or pid == 'None':
        raise ValueError('missing PlayerId')
    return pid, faction, sd


def require_persona(pid):
    if not get_id() or str(get_id()) != pid:
        raise ValueError('sd PlayerId does not match request persona')


def is_casual(body):
    # Client wire constant PvpDataKey.Mode is 'q'.  The other aliases are
    # tolerated only until an actual network capture confirms the exact value.
    raw = next((body[k] for k in ('q', 'mode', 'Mode', 'pvpMode', 'PvpMode')
                if k in body), None)
    return raw is None or str(raw).lower() in ('1', 'casual')


def cleanup_waiting(pid, st):
    if st.get('status') == 'waiting':
        r.zrem(queue_key(st['content'], st['faction']), pid)
    r.delete(player_key(pid), pid + '-sd', pid + '-seed')


@matchmaking.before_request
def content_guard():
    # Compatible content is mandatory for Casual AND the challenge join path,
    # but queryStats cannot add players to Redis and is left lightweight.
    if request.endpoint == 'matchmaking.query_stats':
        return None
    return verify_content_header()


@matchmaking.route('/makeMatch', methods=['POST'])
def make_match():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not is_casual(body):
        return fail('Only Casual matchmaking is implemented')
    try:
        pid, faction, sd = selection(body)
        require_persona(pid)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        return fail(str(e))
    content = g.pvzh_content_digest
    key = player_key(pid)
    with r.lock('contentmatch:queue-lock', timeout=5, blocking_timeout=2):
        current = state(pid)
        if current and current.get('content') != content:
            return fail('CardDataMismatch', 409)
        if current.get('status') == 'matched':
            return paired(current['opponent'])
        if current.get('status') == 'waiting':
            r.expire(key, WAIT_TTL)
            return waiting()
        r.delete(key)
        r.setex(pid + '-sd', MATCH_TTL, sd)
        r.setex(pid + '-seed', MATCH_TTL, str(secrets.randbits(60)))
        r.hset(key, mapping={'content': content, 'faction': faction,
                            'status': 'waiting', 'opponent': '', 'time': str(time.time())})
        r.expire(key, WAIT_TTL)
        opposite = 'Zombies' if faction == 'Plants' else 'Plants'
        q_opp = queue_key(content, opposite)
        while True:
            entries = r.zpopmin(q_opp, 1)
            if not entries:
                break
            opponent_id = txt(entries[0][0])
            opponent = state(opponent_id)
            if (opponent_id == pid or opponent.get('content') != content or
                    opponent.get('status') != 'waiting' or
                    opponent.get('faction') != opposite or
                    r.exists(opponent_id + '-sd', opponent_id + '-seed') != 2):
                continue
            r.hset(player_key(pid), mapping={'status': 'matched', 'opponent': opponent_id})
            r.hset(player_key(opponent_id), mapping={'status': 'matched', 'opponent': pid})
            for p in (pid, opponent_id):
                r.expire(player_key(p), MATCH_TTL)
                r.expire(p + '-sd', MATCH_TTL)
                r.expire(p + '-seed', MATCH_TTL)
            return paired(opponent_id)
        q_me = queue_key(content, faction)
        if r.zcard(q_me) >= MAX_QUEUE:
            cleanup_waiting(pid, state(pid))
            return fail('MatchmakingQueueFull', 429)
        r.zadd(q_me, {pid: time.time()})
        r.expire(q_me, MATCH_TTL)
    return waiting()


@matchmaking.route('/matchPoll', methods=['POST'])
def match_poll():
    pid = get_id()
    st = state(pid)
    if not st or st.get('content') != g.pvzh_content_digest:
        return fail('Not queued or CardDataMismatch', 403)
    if st.get('status') == 'matched':
        return paired(st['opponent'])
    if st.get('status') == 'waiting':
        r.expire(player_key(pid), WAIT_TTL)
        return waiting()
    return fail('MatchmakingExpired', 404)


@matchmaking.route('/joinMatch', methods=['POST'])
def join_match():
    pid = get_id()
    body = request.get_json(silent=True) or {}
    other = str(body.get('gi', ''))
    st = state(pid)
    if st:
        if (st.get('status') != 'matched' or st.get('content') != g.pvzh_content_digest or
                st.get('opponent') != other):
            return fail('Invalid match participant / CardDataMismatch', 403)
        second = state(other)
        if second.get('content') != g.pvzh_content_digest or second.get('opponent') != pid:
            return fail('OpponentCardDataMismatch', 409)
        sd = r.get(other + '-sd')
        if sd is None:
            return fail('Opponent selection data expired', 410)
        return {'ty': 'MatchReady', 'sd': txt(sd), 'gi': other, 'or': 50}
    # Preserve the base server's existing friend challenge behavior.
    sd = it_should_be_there_soon(other + '-sd')
    if not sd:
        return fail('No challenge data', 404)
    return {'ty': 'MatchReady', 'sd': txt(sd), 'gi': other, 'or': 50}


@matchmaking.route('/cancelMatch', methods=['POST'])
def cancel_match():
    pid = get_id()
    with r.lock('contentmatch:queue-lock', timeout=5, blocking_timeout=2):
        st = state(pid)
        if st and st.get('content') == g.pvzh_content_digest:
            cleanup_waiting(pid, st)
    return {}


@matchmaking.route('/queryStats', methods=['GET', 'POST'])
def query_stats():
    # No queue access; returns neutral faction recommendations until expanded.
    return {'fastestCasualMatch': 0, 'fastestRankedMatch': 0}


@matchmaking.route('/createChallenge', methods=['POST'])
def create_challenge():
    data = request.get_json(silent=True) or {}
    try:
        pid, _, sd = selection(data)
        require_persona(pid)
        other = str(data['o'])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        return fail(str(e))
    r.setex(pid + '-sd', MATCH_TTL, sd)
    r.setex(pid + '-seed', MATCH_TTL, str(secrets.randbits(60)))
    return {'ty': 'JoinMatch', 'eta': 0, 'gi': other}
