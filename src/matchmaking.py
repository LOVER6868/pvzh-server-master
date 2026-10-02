import json

from flask import Blueprint, request
from utils import r, it_should_be_there_soon
from random import getrandbits

matchmaking = Blueprint('matchmaking', __name__, url_prefix='/matchmaking/v1')


@matchmaking.route('/createChallenge', methods=['POST'])
def create_challenge():
    data = request.get_json()
    player_data = json.loads(data['sd'])
    challenger = player_data['PlayerId']
    challenged = data['o']

    deck = player_data.get('Deck') or {}
    print(
        '[PVP SD createChallenge]',
        'PlayerId=', player_data.get('PlayerId'),
        'Faction=', player_data.get('Faction'),
        'HeroId=', repr(player_data.get('HeroId')),
        'HeroAssetId=', repr(player_data.get('HeroAssetId')),
        'HeroAssetIdType=', type(player_data.get('HeroAssetId')).__name__,
        'main=', deck.get('main'),
        'super=', deck.get('super'),
        flush=True
    )
    seed_contribution = getrandbits(60)
    r.setex(challenger + "-sd", 20, data['sd'])
    r.setex(challenger + "-seed", 20, seed_contribution)
    return {
        "ty": "JoinMatch",
        "eta": 0,
        "gi": challenged
    }


@matchmaking.route('/joinMatch', methods=['POST'])
def join_match():
    data = request.get_json()
    gi = data['gi']
    return {
        "ty": "MatchReady",
        "sd": it_should_be_there_soon(gi + "-sd").decode(),
        "gi": gi,
        "or": 50
    }


@matchmaking.route('/makeMatch')
def make_match():
    return {
        "ty": "WaitingForMatch",
        "eta": 30,
        "gi": None
    }


@matchmaking.route('/matchPoll')
def match_poll():
    return {}
