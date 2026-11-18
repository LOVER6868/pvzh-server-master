"""PvP initialization and message relay with bounded match lifetime."""
import json
import time

from flask import Blueprint, g, request
from utils import get_id, make_entity_model, make_rng, r
from content_gate import verify_content_header
from matchmaking import (state, txt, MATCH_TTL, JOIN_TTL, GAME_IDLE_TTL,
                         LOCK_NAME, player_key, cleanup_session)

pvp = Blueprint('pvp', __name__, url_prefix='/pvp/v1')


@pvp.before_request
def content_guard():
    if request.endpoint in ('pvp.player_pvp_data', 'pvp.history'):
        return None
    return verify_content_header()


def failure(reason, status=400):
    return {'ty': 'MatchFailed', 'error': reason}, status


def pair():
    pid = get_id()
    st = state(pid)
    if not st:
        return pid, None, False  # preserve the existing friend challenge path
    if (st.get('status') not in ('matched', 'playing') or
            st.get('content') != g.pvzh_content_digest):
        return pid, None, True
    other = st.get('opponent', '')
    peer = state(other)
    if (not st.get('session') or peer.get('session') != st.get('session') or
            peer.get('opponent') != pid or peer.get('content') != st['content'] or
            peer.get('status') not in ('matched', 'playing')):
        return pid, None, True
    return pid, other, True


def touch_caller(pid):
    """Maintain only the connected player's liveness; never revive the opponent."""
    st = state(pid)
    if st:
        r.expire(player_key(pid), GAME_IDLE_TTL if st.get('status') == 'playing' else JOIN_TTL)
        r.expire(pid + '-sd', MATCH_TTL)
        r.expire(pid + '-seed', MATCH_TTL)


@pvp.route('/initGame', methods=['POST'])
def init_game():
    pid, opponent, is_casual = pair()
    body = request.get_json(silent=True) or {}
    other = str(body.get('gi', ''))
    if is_casual and opponent != other:
        return failure('Invalid Casual opponent or CardDataMismatch', 403)
    if not is_casual:
        opponent = other
    if not pid or not opponent:
        return failure('No opponent', 403)
    sd1, sd2 = r.get(pid + '-sd'), r.get(opponent + '-sd')
    seed1, seed2 = r.get(pid + '-seed'), r.get(opponent + '-seed')
    if None in (sd1, sd2, seed1, seed2):
        return failure('Match initialization data missing or expired', 410)
    try:
        rng_seed = int(txt(seed1)) ^ int(txt(seed2))
        entity = make_entity_model(json.loads(sd1), json.loads(sd2))
    except (ValueError, TypeError, KeyError) as exc:
        return failure('Cannot create initial game: ' + str(exc))
    if is_casual:
        # Convert only THIS participant to 'playing'. The other side stays
        # 'matched' until its own /initGame, and will not be kept alive by us.
        with r.lock(LOCK_NAME, timeout=5, blocking_timeout=2):
            st = state(pid)
            peer = state(opponent)
            if (not st.get('session') or st.get('session') != peer.get('session') or
                    st.get('opponent') != opponent):
                return failure('Match ended during initialization', 410)
            r.hset(player_key(pid), mapping={'status': 'playing', 'last_seen': str(time.time())})
            r.expire(player_key(pid), GAME_IDLE_TTL)
        touch_caller(pid)
    return {'entityModel': entity, 'rngSeedData': make_rng(rng_seed),
            'ec': 'CgQgACgA', 'sc': '', 'tec': '', 'cec': 'EAE=',
            'ty': 'InitialGameState', 'plays': '{}', 'gst': '0'}


@pvp.route('/pvpSendUpdate', methods=['POST'])
def pvp_send_update():
    pid, opponent, is_casual = pair()
    body = request.get_json(silent=True) or {}
    recipient = str(body.get('gi', ''))
    if is_casual and opponent != recipient:
        return failure('Wrong recipient or CardDataMismatch', 403)
    if not pid or not recipient:
        return failure('Invalid recipient', 400)
    if not is_casual and r.exists(pid + '-sd', recipient + '-sd') != 2:
        return failure('No active challenge', 403)
    queue = recipient + '-update'
    if r.llen(queue) >= 256:
        return failure('Recipient message queue full', 429)
    r.rpush(queue, json.dumps(body, separators=(',', ':')))
    r.expire(queue, MATCH_TTL)
    if is_casual:
        touch_caller(pid)
    return {'ty': 'PlayResponse', 'p': 'Play'}


@pvp.route('/pvpPoll', methods=['POST'])
def pvp_poll():
    pid, opponent, is_casual = pair()
    if is_casual and not opponent:
        return failure('No active Casual match or CardDataMismatch', 403)
    if not pid:
        return failure('No player', 403)
    body = request.get_json(silent=True) or {}
    try:
        last = max(0, int(body.get('l', 0)))
    except (ValueError, TypeError):
        return failure('Invalid last message cursor', 400)
    update = r.blpop([pid + '-update'], timeout=4)
    if is_casual:
        # Important: polling one player's game cannot extend an absent
        # opponent's Redis session forever (the v2 ghost-match bug).
        touch_caller(pid)
    return {'m': [txt(update[1])] if update else [],
            'l': last + 1 if update else last,
            'ty': 'PvpMessages'}


@pvp.route('/history', methods=['GET'])
def history():
    return {'seasons': {}, 'version': None, 'id': get_id()}


@pvp.route('/playerPvpData', methods=['GET'])
def player_pvp_data():
    return {'gamesPlayed': 0, 'stars': 0, 'id': get_id(), 'version': None,
            'season': 'season_73', 'wins': 0, 'losses': 0, 'mmr': 1600,
            'rank': 50, 'streakBonus': 0, 'wonLastGame': False}


@pvp.route('/winGame', methods=['POST'])
@pvp.route('/gameComplete', methods=['POST'])
def complete_game():
    # The original prototype acknowledged completion without removing either
    # player's matched state. This stranded the pair in Redis indefinitely.
    pid = get_id()
    with r.lock(LOCK_NAME, timeout=5, blocking_timeout=2):
        st = state(pid)
        if (st and st.get('content') == g.pvzh_content_digest and
                st.get('status') in ('matched', 'playing')):
            cleanup_session(pid)  # remove BOTH sides atomically
    return {}
