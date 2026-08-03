import math
import re
from collections import defaultdict
from dataclasses import dataclass

import numpy


RATING_SCALE = math.log(0.76 / 0.24) / 202.0
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20260723
PENTA_PAIR_SCORES = numpy.array((0.0, 0.5, 1.0, 1.5, 2.0))
RATING_TEST_MODES = ('SPRT', 'GAMES')


class RatingInputError(ValueError):
    pass


class RatingConvergenceError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class AiKey:
    network: str
    commit: str


@dataclass(frozen=True)
class Observation:
    test_id: int
    dev: AiKey
    base: AiKey
    wins: int
    draws: int
    losses: int
    ptnml: tuple

    @property
    def games(self):
        return self.wins + self.draws + self.losses

    @property
    def score(self):
        return self.wins + self.draws / 2.0

    @property
    def pairs(self):
        return sum(self.ptnml)


@dataclass(frozen=True)
class MatchScore:
    dev_index: int
    base_index: int
    games: int
    score: float


@dataclass(frozen=True)
class PlayerRating:
    key: AiKey
    label: str
    aliases: tuple
    rating: float
    lower: float
    upper: float
    games: int
    first_place_rate: float


@dataclass(frozen=True)
class HeadToHead:
    player_a: AiKey
    player_b: AiKey
    label_a: str
    label_b: str
    wins: int
    draws: int
    losses: int
    direct_elo: float
    fitted_elo: float
    residual: float


@dataclass(frozen=True)
class RatingAnalysis:
    players: tuple
    head_to_head: tuple
    cfs: tuple
    warnings: tuple
    bootstrap_samples: int


def parse_test_ids(raw):
    tokens = [token for token in re.split(r'[\s,]+', (raw or '').strip()) if token]
    if not tokens:
        raise RatingInputError('テスト番号を1件以上入力してください。')

    test_ids = []
    seen = set()
    for token in tokens:
        if not token.isdigit() or int(token) <= 0:
            raise RatingInputError('テスト番号は正の整数で指定してください: %s' % token)
        test_id = int(token)
        if test_id not in seen:
            seen.add(test_id)
            test_ids.append(test_id)
    return test_ids


def ai_key(test, side):
    engine = getattr(test, side)
    return AiKey(
        (getattr(test, side + '_network') or '').strip(),
        (engine.sha or '').strip(),
    )


def ai_label(test, side):
    display = (getattr(test, side + '_display') or '').strip()
    if display:
        return display

    engine = getattr(test, side)
    label = (engine.name or getattr(test, side + '_engine') or side).strip()
    netname = (getattr(test, side + '_netname') or '').strip()
    if netname and netname != label:
        label += ' / ' + netname
    return label


def observation_from_test(test):
    if test.test_mode not in RATING_TEST_MODES:
        raise RatingInputError(
            'Test %dはSPRT/GAMESではありません: %s' % (test.id, test.test_mode))

    values = (
        test.games, test.wins, test.draws, test.losses,
        test.LL, test.LD, test.DD, test.DW, test.WW,
    )
    if any(value < 0 for value in values):
        raise RatingInputError('Test %dの集計値に負数があります。' % test.id)

    ptnml = (test.LL, test.LD, test.DD, test.DW, test.WW)
    games = test.wins + test.draws + test.losses
    if games == 0:
        raise RatingInputError('Test %dには完了対局がありません。' % test.id)
    if games != test.games:
        raise RatingInputError(
            'Test %dの対局数とW/D/Lが一致しません。' % test.id)
    if 2 * sum(ptnml) != games:
        raise RatingInputError(
            'Test %dの対局数とPtnmlが一致しません。' % test.id)

    wdl_half_points = 2 * test.wins + test.draws
    ptnml_half_points = test.LD + 2 * test.DD + 3 * test.DW + 4 * test.WW
    if wdl_half_points != ptnml_half_points:
        raise RatingInputError(
            'Test %dのW/D/LとPtnmlの得点が一致しません。' % test.id)

    return Observation(
        test.id,
        ai_key(test, 'dev'),
        ai_key(test, 'base'),
        test.wins,
        test.draws,
        test.losses,
        ptnml,
    )


def _connected_components(player_count, matches):
    neighbours = [set() for _ in range(player_count)]
    for match in matches:
        neighbours[match.dev_index].add(match.base_index)
        neighbours[match.base_index].add(match.dev_index)

    unseen = set(range(player_count))
    components = []
    while unseen:
        start = min(unseen)
        stack = [start]
        unseen.remove(start)
        component = []
        while stack:
            player = stack.pop()
            component.append(player)
            for neighbour in neighbours[player]:
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    stack.append(neighbour)
        components.append(tuple(sorted(component)))
    return tuple(components)


def _log_likelihood(ratings, matches):
    total = 0.0
    for match in matches:
        value = RATING_SCALE * (
            ratings[match.dev_index] - ratings[match.base_index])
        total += (
            -match.score * numpy.logaddexp(0.0, -value)
            -(match.games - match.score) * numpy.logaddexp(0.0, value)
        )
    return float(total)


def solve_ratings(player_count, matches, tolerance=1e-9, max_iterations=100):
    matches = tuple(matches)
    if player_count < 2:
        raise RatingInputError('参加AIが2種類未満です。')
    if not matches:
        raise RatingInputError('レーティング情報を持つ対戦がありません。')

    components = _connected_components(player_count, matches)
    if len(components) != 1:
        raise RatingInputError('参加AIの対戦グラフが連結していません。')

    ratings = numpy.zeros(player_count, dtype=float)
    for _ in range(max_iterations):
        gradient = numpy.zeros(player_count, dtype=float)
        information = numpy.zeros((player_count, player_count), dtype=float)

        for match in matches:
            difference = ratings[match.dev_index] - ratings[match.base_index]
            value = RATING_SCALE * difference
            probability = (
                1.0 / (1.0 + math.exp(-value))
                if value >= 0
                else math.exp(value) / (1.0 + math.exp(value))
            )

            error = RATING_SCALE * (
                match.score - match.games * probability)
            gradient[match.dev_index] += error
            gradient[match.base_index] -= error

            weight = (
                RATING_SCALE * RATING_SCALE * match.games
                * probability * (1.0 - probability)
            )
            information[match.dev_index, match.dev_index] += weight
            information[match.base_index, match.base_index] += weight
            information[match.dev_index, match.base_index] -= weight
            information[match.base_index, match.dev_index] -= weight

        try:
            reduced_step = numpy.linalg.solve(
                information[:-1, :-1], gradient[:-1])
        except numpy.linalg.LinAlgError as error:
            raise RatingConvergenceError(
                'レーティング計算の連立方程式を解けません。') from error

        step = numpy.zeros(player_count, dtype=float)
        step[:-1] = reduced_step
        baseline = _log_likelihood(ratings, matches)
        step_scale = 1.0

        for _ in range(25):
            candidate = ratings + step_scale * step
            candidate -= candidate.mean()
            if _log_likelihood(candidate, matches) >= baseline - 1e-10:
                break
            step_scale /= 2.0
        else:
            raise RatingConvergenceError(
                'レーティング計算の尤度を改善できません。')

        change = float(numpy.max(numpy.abs(candidate - ratings)))
        ratings = candidate
        if not numpy.all(numpy.isfinite(ratings)) or numpy.max(numpy.abs(ratings)) > 100_000:
            raise RatingConvergenceError(
                '有限のレーティングを推定できません。')
        if change < tolerance:
            return ratings

    raise RatingConvergenceError(
        'レーティング計算が%d回以内に収束しません。' % max_iterations)


def _condition_value(test, attribute):
    dev_value = getattr(test, 'dev_' + attribute)
    base_value = getattr(test, 'base_' + attribute)
    return (
        str(dev_value)
        if dev_value == base_value
        else 'dev=%s / base=%s' % (dev_value, base_value)
    )


def _option_value(test, name):
    pattern = re.compile(
        r'(?:^|\s)%s=(?:"([^"]*)"|\'([^\']*)\'|([^\s]*))'
        % re.escape(name))

    def extract(options):
        match = pattern.search(options or '')
        if not match:
            return '未指定'
        return next(value for value in match.groups() if value is not None)

    dev_value = extract(test.dev_options)
    base_value = extract(test.base_options)
    return (
        dev_value
        if dev_value == base_value
        else 'dev=%s / base=%s' % (dev_value, base_value)
    )


def _grouped_difference(tests, label, value_getter):
    groups = defaultdict(list)
    for test in tests:
        groups[str(value_getter(test))].append(test.id)
    if len(groups) <= 1:
        return None

    values = []
    for value, test_ids in sorted(groups.items()):
        values.append('%s (Test %s)' % (
            value, ', '.join(str(test_id) for test_id in sorted(test_ids))))
    return '%s: %s' % (label, '; '.join(values))


def condition_warnings(tests):
    checks = (
        ('開始局面集', lambda test: test.book_name),
        ('制限時間', lambda test: _condition_value(test, 'time_control')),
        ('Threads', lambda test: _option_value(test, 'Threads')),
        ('Hash', lambda test: _option_value(test, 'Hash')),
        ('Ponder', lambda test: _condition_value(test, 'ponder_mode')),
        ('NPSスケール', lambda test: '%s/%s' % (test.scale_method, test.scale_nps)),
        ('勝敗判定', lambda test: test.win_adj),
        ('引分判定', lambda test: test.draw_adj),
        ('Syzygy WDL', lambda test: test.syzygy_wdl),
        ('Syzygy adjudication', lambda test: test.syzygy_adj),
    )
    warnings = [
        warning
        for label, getter in checks
        for warning in [_grouped_difference(tests, label, getter)]
        if warning is not None
    ]

    metadata = defaultdict(lambda: {'labels': set(), 'build_args': set()})
    for test in tests:
        for side in ('dev', 'base'):
            key = ai_key(test, side)
            metadata[key]['labels'].add(ai_label(test, side))
            metadata[key]['build_args'].add(
                (getattr(test, side + '_build_args') or '').strip() or '未指定')

    for key, values in sorted(metadata.items()):
        short_key = '%s / %s' % (
            key.network or 'networkなし',
            key.commit[:12] or 'commitなし',
        )
        if len(values['labels']) > 1:
            warnings.append(
                '同じAIキーの表示名が異なります (%s): %s'
                % (short_key, ', '.join(sorted(values['labels']))))
        if len(values['build_args']) > 1:
            warnings.append(
                '同じAIキーのビルド引数が異なります (%s): %s'
                % (short_key, ', '.join(sorted(values['build_args']))))

    deleted = sorted(test.id for test in tests if test.deleted)
    if deleted:
        warnings.append(
            '削除済みテストを含みます: Test %s'
            % ', '.join(map(str, deleted)))

    running = sorted(test.id for test in tests if not test.finished)
    if running:
        warnings.append(
            '実行中のテストを含むため、結果が変化する可能性があります: Test %s'
            % ', '.join(map(str, running)))

    return tuple(warnings)


def _bootstrap_ratings(player_count, observations, player_indices, samples, seed):
    generator = numpy.random.default_rng(seed)
    output = numpy.empty((samples, player_count), dtype=float)

    for sample_index in range(samples):
        matches = []
        for observation in observations:
            probabilities = numpy.asarray(observation.ptnml, dtype=float)
            probabilities /= observation.pairs
            counts = generator.multinomial(observation.pairs, probabilities)
            score = float(counts @ PENTA_PAIR_SCORES)
            matches.append(MatchScore(
                player_indices[observation.dev],
                player_indices[observation.base],
                2 * observation.pairs,
                score,
            ))
        try:
            output[sample_index] = solve_ratings(player_count, matches)
        except RatingConvergenceError as error:
            raise RatingConvergenceError(
                'Ptnml bootstrapの標本%dで推定できません: %s'
                % (sample_index + 1, error)) from error
    return output


def _direct_elo(wins, draws, losses):
    games = wins + draws + losses
    score_rate = (wins + draws / 2.0) / games
    if score_rate <= 0.0:
        return -math.inf
    if score_rate >= 1.0:
        return math.inf
    return math.log(score_rate / (1.0 - score_rate)) / RATING_SCALE


def analyze_tests(tests, bootstrap_samples=BOOTSTRAP_SAMPLES, seed=BOOTSTRAP_SEED):
    tests = tuple(sorted(tests, key=lambda test: test.id))
    observations = tuple(observation_from_test(test) for test in tests)

    labels = defaultdict(list)
    for test in tests:
        for side in ('dev', 'base'):
            entry = ai_label(test, side)
            key = ai_key(test, side)
            if entry not in labels[key]:
                labels[key].append(entry)

    selfplay_ids = sorted(
        observation.test_id
        for observation in observations
        if observation.dev == observation.base
    )
    observations = tuple(
        observation
        for observation in observations
        if observation.dev != observation.base
    )

    players = tuple(sorted({
        key
        for observation in observations
        for key in (observation.dev, observation.base)
    }))
    if len(players) < 2:
        raise RatingInputError('参加AIが2種類未満です。')

    player_indices = {player: index for index, player in enumerate(players)}
    matches = tuple(
        MatchScore(
            player_indices[observation.dev],
            player_indices[observation.base],
            observation.games,
            observation.score,
        )
        for observation in observations
    )

    components = _connected_components(len(players), matches)
    if len(components) != 1:
        component_labels = []
        for number, component in enumerate(components, start=1):
            names = [labels[players[index]][0] for index in component]
            component_labels.append(
                '成分%d: %s' % (number, ', '.join(names)))
        raise RatingInputError(
            '参加AIの対戦グラフが連結していません。%s'
            % ' / '.join(component_labels))

    fitted = solve_ratings(len(players), matches)
    bootstrap = _bootstrap_ratings(
        len(players), observations, player_indices, bootstrap_samples, seed)
    lower = numpy.percentile(bootstrap, 2.5, axis=0)
    upper = numpy.percentile(bootstrap, 97.5, axis=0)

    games_by_player = [0] * len(players)
    for match in matches:
        games_by_player[match.dev_index] += match.games
        games_by_player[match.base_index] += match.games

    first_place = numpy.zeros(len(players), dtype=float)
    winners = numpy.argmax(bootstrap, axis=1)
    for index in range(len(players)):
        first_place[index] = numpy.mean(winners == index)

    player_rows = []
    for index, player in enumerate(players):
        player_rows.append(PlayerRating(
            player,
            labels[player][0],
            tuple(labels[player][1:]),
            float(fitted[index]),
            float(lower[index]),
            float(upper[index]),
            games_by_player[index],
            float(first_place[index]),
        ))
    player_rows.sort(key=lambda player: player.rating, reverse=True)

    direct = defaultdict(lambda: [0, 0, 0])
    for observation in observations:
        dev_index = player_indices[observation.dev]
        base_index = player_indices[observation.base]
        if dev_index < base_index:
            key = (observation.dev, observation.base)
            values = (observation.wins, observation.draws, observation.losses)
        else:
            key = (observation.base, observation.dev)
            values = (observation.losses, observation.draws, observation.wins)
        for index, value in enumerate(values):
            direct[key][index] += value

    head_to_head = []
    for (player_a, player_b), (wins, draws, losses) in sorted(direct.items()):
        index_a = player_indices[player_a]
        index_b = player_indices[player_b]
        direct_elo = _direct_elo(wins, draws, losses)
        fitted_elo = float(fitted[index_a] - fitted[index_b])
        head_to_head.append(HeadToHead(
            player_a,
            player_b,
            labels[player_a][0],
            labels[player_b][0],
            wins,
            draws,
            losses,
            direct_elo,
            fitted_elo,
            direct_elo - fitted_elo,
        ))

    cfs = []
    for player_a in player_rows:
        index_a = player_indices[player_a.key]
        row = []
        for player_b in player_rows:
            index_b = player_indices[player_b.key]
            if index_a == index_b:
                row.append(0.5)
            else:
                greater = bootstrap[:, index_a] > bootstrap[:, index_b]
                equal = bootstrap[:, index_a] == bootstrap[:, index_b]
                row.append(float(numpy.mean(greater) + 0.5 * numpy.mean(equal)))
        cfs.append(tuple(row))

    warnings = list(condition_warnings(tests))
    if selfplay_ids:
        warnings.append(
            '同一AIキー同士の自己対局を除外しました: Test %s'
            % ', '.join(map(str, selfplay_ids)))

    return RatingAnalysis(
        tuple(player_rows),
        tuple(head_to_head),
        tuple(cfs),
        tuple(warnings),
        bootstrap_samples,
    )
