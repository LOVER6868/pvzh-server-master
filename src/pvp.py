import json

from flask import Blueprint, request

from utils import get_id, it_should_be_there_soon, make_entity_model, make_rng, r

pvp = Blueprint('pvp', __name__, url_prefix='/pvp/v1')


@pvp.route('/initGame', methods=['POST'])
def init_game():
    pid = get_id()
    data = request.get_json()
    gi = data['gi']
    seed1 = it_should_be_there_soon(pid + '-seed')
    seed2 = it_should_be_there_soon(gi + '-seed')
    seed = int.from_bytes(seed1) ^ int.from_bytes(seed2)
    sd1 = it_should_be_there_soon(pid + '-sd')
    sd2 = it_should_be_there_soon(gi + '-sd')
    sd1_json = json.loads(sd1)
    sd2_json = json.loads(sd2)

    print(
        '[PVP initGame sd1]',
        'PlayerId=', sd1_json.get('PlayerId'),
        'Faction=', sd1_json.get('Faction'),
        'HeroId=', repr(sd1_json.get('HeroId')),
        'HeroAssetId=', repr(sd1_json.get('HeroAssetId')),
        'super=', (sd1_json.get('Deck') or {}).get('super'),
        flush=True
    )
    print(
        '[PVP initGame sd2]',
        'PlayerId=', sd2_json.get('PlayerId'),
        'Faction=', sd2_json.get('Faction'),
        'HeroId=', repr(sd2_json.get('HeroId')),
        'HeroAssetId=', repr(sd2_json.get('HeroAssetId')),
        'super=', (sd2_json.get('Deck') or {}).get('super'),
        flush=True
    )

    return {
        "entityModel": make_entity_model(sd1_json, sd2_json),
        "rngSeedData": make_rng(seed),
        "ec": "CgQgACgA",
        "sc": "",
        "tec": "",
        "cec": "EAE=",
        "ty": "InitialGameState",
        "plays": "{}",
        "gst": "0"
    }


@pvp.route('/pvpSendUpdate', methods=['POST'])
def pvp_send_update():
    data = request.get_json()
    gi = data['gi']
    r.rpush(gi + '-update', json.dumps(data))
    r.expire(gi + '-update', 300)
    return {
        "ty": "PlayResponse",
        "p": "Play"
    }


@pvp.route('/pvpPoll', methods=['POST'])
def pvp_poll():
    pid = get_id()
    data = request.get_json()
    l = int(data['l'])
    update = r.blpop([pid + '-update'], 4)
    if update:
        return {
            "m": [update[1].decode()],
            "l": l + 1,
            "ty": "PvpMessages"
        }
    else:
        return {
            "m": [],
            "l": 0,
            "ty": "PvpMessages"
        }


@pvp.route('/history')
def make_match():
    return {
        "seasons": {},
        "version": None,
        "id": get_id()
    }


@pvp.route('/playerPvpData')
def player_pvp_data():
    return {
        "gamesPlayed": 0,
        "stars": 0,
        "id": get_id(),
        "version": None,
        "season": "season_73",
        "wins": 0,
        "losses": 0,
        "mmr": 1600,
        "rank": 50,
        "streakBonus": 0,
        "wonLastGame": False
    }
