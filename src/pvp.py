"""PvP init and relay with card content matching for Casual participants."""
import json

from flask import Blueprint, g, request
from utils import get_id, make_entity_model, make_rng, r
from content_gate import verify_content_header
from matchmaking import state, txt, MATCH_TTL

pvp = Blueprint('pvp', __name__, url_prefix='/pvp/v1')


@pvp.before_request
def content_guard():
    # Existing account display routes don't initialize games or relay updates.
    if request.endpoint in ('pvp.player_pvp_data', 'pvp.history'):
        return None
    return verify_content_header()


def failure(reason, status=400):
    return {'ty': 'MatchFailed', 'error': reason}, status


def pair():
    pid = get_id()
    st = state(pid)
    if not st:
        return pid, None, False  # existing friend challenge protocol
    if st.get('status') != 'matched' or st.get('content') != g.pvzh_content_digest:
        return pid, None, True
    other = st['opponent']
    opposite = state(other)
    if (opposite.get('status') != 'matched' or opposite.get('opponent') != pid or
            opposite.get('content') != st['content']):
        return pid, None, True
    return pid, other, True


def touch(pid, other):
    for p in (pid, other):
        r.expire(f'contentmatch:player:{p}', MATCH_TTL)
        r.expire(p + '-sd', MATCH_TTL)
        r.expire(p + '-seed', MATCH_TTL)


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
    except (ValueError, TypeError, KeyError) as e:
        return failure('Cannot create initial game: ' + str(e))
    if is_casual:
        touch(pid, opponent)
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
    if not is_casual and not r.exists(pid + '-sd', recipient + '-sd') == 2:
        return failure('No active challenge', 403)
    queue = recipient + '-update'
    if r.llen(queue) >= 256:
        return failure('Recipient message queue full', 429)
    r.rpush(queue, json.dumps(body, separators=(',', ':')))
    r.expire(queue, MATCH_TTL)
    if is_casual:
        touch(pid, opponent)
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
        touch(pid, opponent)
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
    # Demo: acknowledges only. Rank/reward implementation not included.
    return {}
