import hashlib
import json
from dataclasses import dataclass


RATING_TEST_MODES = ('SPRT', 'GAMES')


class InconsistentResultError(ValueError):
    pass


@dataclass(frozen=True)
class SyntheticGame:
    test_id: int
    pair_index: int
    game_index: int
    white: str
    black: str
    result: str
    deleted: bool
    date: str
    book: str
    time_control: str
    recorded_time_control: str
    dev_options: str
    base_options: str


def player_key(test, side):
    """同一エンジンを、テスト内のdev/baseという役割から独立して識別する。"""

    engine = getattr(test, side)
    return (
        getattr(test, side + '_engine'),
        getattr(test, side + '_repo'),
        engine.sha,
        getattr(test, side + '_network'),
        getattr(test, side + '_build_args'),
    )


def player_fingerprint(key):
    encoded = json.dumps(key, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:10]


def player_label(test, side):
    """Ordo表で読める名前にしつつ、異なる構成を短い指紋で区別する。"""

    engine = getattr(test, side)
    netname = getattr(test, side + '_netname')
    friendly = engine.name or getattr(test, side + '_engine')
    if netname and netname != friendly:
        friendly += ' / ' + netname
    return '%s [%s]' % (friendly[:180], player_fingerprint(player_key(test, side)))


def reconstructed_pairs(test):
    """PentanomialとTrinomialから、dev視点の2局一組の結果を復元する。"""

    pair_count = test.LL + test.LD + test.DD + test.DW + test.WW
    mixed_wl_from_losses = test.losses - 2 * test.LL - test.LD
    mixed_wl_from_wins = test.wins - 2 * test.WW - test.DW
    draw_draw = test.DD - mixed_wl_from_losses

    consistent = (
        test.losses + test.draws + test.wins == test.games
        and 2 * pair_count == test.games
        and mixed_wl_from_losses == mixed_wl_from_wins
        and 0 <= mixed_wl_from_losses <= test.DD
        and draw_draw >= 0
    )
    if not consistent:
        raise InconsistentResultError(
            'Test %d has inconsistent trinomial/pentanomial counts' % test.id)

    # 非対称なペアは向きを交互にし、架空の先後勝率を作らない。
    flip = False
    groups = (
        (test.LL, 'L', 'L'),
        (test.LD, 'L', 'D'),
        (draw_draw, 'D', 'D'),
        (mixed_wl_from_losses, 'L', 'W'),
        (test.DW, 'D', 'W'),
        (test.WW, 'W', 'W'),
    )

    for count, first, second in groups:
        for _ in range(count):
            if first != second and flip:
                yield second, first
            else:
                yield first, second
            if first != second:
                flip = not flip


def result_token(dev_outcome, dev_is_white):
    if dev_outcome == 'D':
        return '1/2-1/2'
    dev_won = dev_outcome == 'W'
    white_won = dev_won == dev_is_white
    return '1-0' if white_won else '0-1'


def synthetic_games(tests, time_control_overrides=None):
    tests = list(tests)
    time_control_overrides = time_control_overrides or {}

    # 同じ構成が別テストで異なる表示名を持っても、最初の名前へ統合する。
    names = {}
    for test in tests:
        for side in ('dev', 'base'):
            names.setdefault(player_key(test, side), player_label(test, side))

    for test in tests:
        dev_key = player_key(test, 'dev')
        base_key = player_key(test, 'base')
        if dev_key == base_key:
            continue

        dev_name = names[dev_key]
        base_name = names[base_key]
        date = test.creation.strftime('%Y.%m.%d') if test.creation else '????.??.??'
        effective_time_control = time_control_overrides.get(test.id, test.dev_time_control)

        for pair_index, outcomes in enumerate(reconstructed_pairs(test), start=1):
            yield SyntheticGame(
                test.id, pair_index, 1,
                dev_name, base_name, result_token(outcomes[0], True),
                test.deleted, date, test.book_name,
                effective_time_control, test.dev_time_control,
                test.dev_options, test.base_options)
            yield SyntheticGame(
                test.id, pair_index, 2,
                base_name, dev_name, result_token(outcomes[1], False),
                test.deleted, date, test.book_name,
                effective_time_control, test.dev_time_control,
                test.dev_options, test.base_options)


def escape_pgn_tag(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def format_pgn(game):
    tags = (
        ('Event', 'ShogiBench reconstructed result'),
        ('Site', 'https://shogibench.fly.dev/test/%d/' % game.test_id),
        ('Date', game.date),
        ('Round', '%d.%d.%d' % (game.test_id, game.pair_index, game.game_index)),
        ('White', game.white),
        ('Black', game.black),
        ('Result', game.result),
        ('ShogiBenchTest', game.test_id),
        ('ShogiBenchDeleted', int(game.deleted)),
        ('ShogiBenchBook', game.book),
        ('ShogiBenchTimeControl', game.time_control),
        ('ShogiBenchRecordedTimeControl', game.recorded_time_control),
        ('ShogiBenchDevOptions', game.dev_options),
        ('ShogiBenchBaseOptions', game.base_options),
        ('Synthetic', 1),
    )
    header = '\n'.join('[%s "%s"]' % (key, escape_pgn_tag(value)) for key, value in tags)
    return '%s\n\n%s\n\n' % (header, game.result)
