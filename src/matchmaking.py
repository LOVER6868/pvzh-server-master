"""Content-matched Casual PvP queue with complete match lifecycle cleanup.

Uses the existing stock PvZH matchmaking response types. All Redis modifications
that create or destroy a pair run under the same short-lived queue lock.
"""
import json
import secrets
import time

from flask import Blueprint, g, request
from utils import get_id, it_should_be_there_soon, r
from content_gate import verify_content_header

matchmaking = Blueprint('matchmaking', __name__, url_prefix='/matchmaking/v1')

# A waiting player must continue polling to keep a queue slot. A matched player
# must finish the join/init handshake within JOIN_TTL. Once playing, pvp.py
# refreshes only the *calling player's* GAME_IDLE_TTL (never the opponent's).
WAIT_TTL = 90
JOIN_TTL = 120
GAME_IDLE_TTL = 180
MATCH_TTL = 900  # selection data / RNG seed storage (not player liveness)
WAIT_ETA = 5
MAX_QUEUE = 200
LOCK_NAME = 'contentmatch:queue-lock'


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
    raw = next((body[k] for k in ('q', 'mode', 'Mode', 'pvpMode', 'PvpMode')
                if k in body), None)
    return raw is None or str(raw).lower() in ('1', 'casual')


def recently_seen(st, seconds):
    try:
        return time.time() - float(st['last_seen']) < seconds
    except (KeyError, ValueError, TypeError):
        return False  # old v2 states had no liveness tracking; discard them


def _remove_player(pid, st):
    """Delete one player's queue, selection, seed and old PvP messages."""
    content = st.get('content')
    if content:
        # Remove from BOTH factions: also repairs leftover zset entries from v2.
        for faction in ('Plants', 'Zombies'):
            r.zrem(queue_key(content, faction), pid)
    r.delete(player_key(pid), pid + '-sd', pid + '-seed', pid + '-update')


def cleanup_session(pid):
    """Delete both sides of a reciprocal match, or one unpaired queue ticket.

    CALL ONLY WHILE HOLDING LOCK_NAME. Matching session IDs prevent an old
    player's cleanup from deleting an opponent's NEW unrelated session.
    """
    st = state(pid)
    if not st:
        return False
    other = st.get('opponent', '')
    session = st.get('session', '')
    peer = state(other) if other and other != pid else {}
    reciprocal = bool(session and peer and peer.get('session') == session and
                      peer.get('opponent') == pid and
                      peer.get('content') == st.get('content'))
    _remove_player(pid, st)
    if reciprocal:
        _remove_player(other, peer)
    return True


def prune_queue(content, faction):
    """Remove members that stopped polling, including old v2 ghost entries."""
    r.zremrangebyscore(queue_key(content, faction), '-inf', time.time() - WAIT_TTL)


def keep_waiting(pid, st):
    now = time.time()
    key = player_key(pid)
    r.hset(key, mapping={'last_seen': str(now)})
    r.expire(key, WAIT_TTL)
    # Queue score represents last heartbeat, not original insertion time.
    q = queue_key(st['content'], st['faction'])
    prune_queue(st['content'], st['faction'])
    r.zadd(q, {pid: now})
    r.expire(q, WAIT_TTL + 5)


@matchmaking.before_request
def content_guard():
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
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return fail(str(exc))

    content = g.pvzh_content_digest
    with r.lock(LOCK_NAME, timeout=5, blocking_timeout=2):
        current = state(pid)
        if current and current.get('content') != content:
            cleanup_session(pid)  # old build must never be reused
            current = {}

        if current.get('status') == 'waiting' and current.get('faction') == faction:
            if recently_seen(current, WAIT_TTL):
                keep_waiting(pid, current)
                return waiting()
            cleanup_session(pid)
            current = {}

        if current.get('status') == 'matched':
            other = current.get('opponent', '')
            peer = state(other)
            if (recently_seen(current, JOIN_TTL) and
                    peer.get('session') == current.get('session') and
                    current.get('session') and peer.get('opponent') == pid):
                return paired(other)  # legitimate retry during the join handshake
            cleanup_session(pid)
            current = {}

        if current:
            # 'playing' means the user has left the previous battle and pressed
            # Casual again. Never return their old JoinMatch response.
            cleanup_session(pid)

        r.delete(player_key(pid), pid + '-update')
        r.setex(pid + '-sd', MATCH_TTL, sd)
        r.setex(pid + '-seed', MATCH_TTL, str(secrets.randbits(60)))

        opposite = 'Zombies' if faction == 'Plants' else 'Plants'
        prune_queue(content, opposite)
        q_opp = queue_key(content, opposite)
        opponent_id = None
        while True:
            entries = r.zpopmin(q_opp, 1)
            if not entries:
                break
            candidate = txt(entries[0][0])
            old = state(candidate)
            if (candidate == pid or old.get('content') != content or
                    old.get('status') != 'waiting' or old.get('faction') != opposite or
                    not recently_seen(old, WAIT_TTL) or
                    r.exists(candidate + '-sd', candidate + '-seed') != 2):
                continue
            opponent_id = candidate
            break

        now = str(time.time())
        if opponent_id:
            session = secrets.token_hex(16)
            for p, fac, other in ((pid, faction, opponent_id),
                                  (opponent_id, opposite, pid)):
                r.hset(player_key(p), mapping={
                    'content': content, 'faction': fac, 'status': 'matched',
                    'opponent': other, 'session': session,
                    'last_seen': now,
                })
                r.expire(player_key(p), JOIN_TTL)
                r.expire(p + '-sd', MATCH_TTL)
                r.expire(p + '-seed', MATCH_TTL)
                r.delete(p + '-update')  # never replay a previous battle
            return paired(opponent_id)

        q_me = queue_key(content, faction)
        prune_queue(content, faction)
        if r.zcard(q_me) >= MAX_QUEUE:
            r.delete(pid + '-sd', pid + '-seed')
            return fail('MatchmakingQueueFull', 429)
        r.hset(player_key(pid), mapping={
            'content': content, 'faction': faction, 'status': 'waiting',
            'opponent': '', 'session': '', 'last_seen': now,
        })
        r.expire(player_key(pid), WAIT_TTL)
        r.zadd(q_me, {pid: float(now)})
        r.expire(q_me, WAIT_TTL + 5)
    return waiting()


@matchmaking.route('/matchPoll', methods=['POST'])
def match_poll():
    pid = get_id()
    with r.lock(LOCK_NAME, timeout=5, blocking_timeout=2):
        st = state(pid)
        if not st or st.get('content') != g.pvzh_content_digest:
            return fail('Not queued or CardDataMismatch', 403)
        if st.get('status') == 'waiting':
            if not recently_seen(st, WAIT_TTL):
                cleanup_session(pid)
                return fail('MatchmakingExpired', 410)
            keep_waiting(pid, st)
            return waiting()
        if st.get('status') == 'matched':
            peer = state(st.get('opponent'))
            if (not recently_seen(st, JOIN_TTL) or
                    not st.get('session') or peer.get('session') != st.get('session') or
                    peer.get('opponent') != pid):
                cleanup_session(pid)
                return fail('OpponentDisconnected', 410)
            r.hset(player_key(pid), mapping={'last_seen': str(time.time())})
            r.expire(player_key(pid), JOIN_TTL)
            return paired(st['opponent'])
        # A lingering matchmaking poll after game init must not restart it.
        return fail('AlreadyInGame', 409)


@matchmaking.route('/joinMatch', methods=['POST'])
def join_match():
    pid = get_id()
    body = request.get_json(silent=True) or {}
    other = str(body.get('gi', ''))
    st = state(pid)
    if st:
        if (st.get('status') not in ('matched', 'playing') or
                st.get('content') != g.pvzh_content_digest or
                st.get('opponent') != other):
            return fail('Invalid match participant / CardDataMismatch', 403)
        second = state(other)
        if (second.get('content') != g.pvzh_content_digest or
                second.get('opponent') != pid or
                not st.get('session') or st.get('session') != second.get('session')):
            return fail('OpponentDisconnected', 410)
        sd = r.get(other + '-sd')
        if sd is None:
            return fail('Opponent selection data expired', 410)
        r.expire(player_key(pid), GAME_IDLE_TTL if st['status'] == 'playing' else JOIN_TTL)
        return {'ty': 'MatchReady', 'sd': txt(sd), 'gi': other, 'or': 50}
    # Base server friend challenge support.
    sd = it_should_be_there_soon(other + '-sd')
    if not sd:
        return fail('No challenge data', 404)
    return {'ty': 'MatchReady', 'sd': txt(sd), 'gi': other, 'or': 50}


@matchmaking.route('/cancelMatch', methods=['POST'])
def cancel_match():
    pid = get_id()
    with r.lock(LOCK_NAME, timeout=5, blocking_timeout=2):
        st = state(pid)
        if st and st.get('content') == g.pvzh_content_digest:
            cleanup_session(pid)  # both sides of a reciprocal match, not just us
    return {}


@matchmaking.route('/queryStats', methods=['GET', 'POST'])
def query_stats():
    return {'fastestCasualMatch': 0, 'fastestRankedMatch': 0}


@matchmaking.route('/createChallenge', methods=['POST'])
def create_challenge():
    data = request.get_json(silent=True) or {}
    try:
        pid, _, sd = selection(data)
        require_persona(pid)
        other = str(data['o'])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return fail(str(exc))
    with r.lock(LOCK_NAME, timeout=5, blocking_timeout=2):
        if state(pid):
            cleanup_session(pid)
        r.setex(pid + '-sd', MATCH_TTL, sd)
        r.setex(pid + '-seed', MATCH_TTL, str(secrets.randbits(60)))
    return {'ty': 'JoinMatch', 'eta': 0, 'gi': other}
