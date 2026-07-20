const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');


function loadScript(testPresets) {
    const elements = {
        'json-config': {
            textContent: JSON.stringify({
                engines: {
                    engine: {
                        test_presets: testPresets,
                        tune_presets: {default: {}},
                        datagen_presets: {default: {}},
                    },
                },
            }),
        },
        'json-networks': {textContent: '[]'},
        'json-repos': {textContent: '{}'},
        'json-build-variants': {textContent: '{}'},
        'json-tune-kits': {textContent: '[]'},
    };
    const context = {
        console,
        document: {
            getElementById(id) {
                return elements[id] || null;
            },
        },
    };
    vm.createContext(context);
    const script = fs.readFileSync(
        path.join(__dirname, '..', 'OpenBench', 'static', 'create_workload.js'),
        'utf8',
    );
    vm.runInContext(script, context);
    return context;
}


const presets = {
    default: {
        base_branch: 'master',
        book_name: 'default.epd',
        test_bounds: '[0.00, 4.00]',
        test_confidence: '[0.10, 0.05]',
        win_adj: 'movecount=3 score=2000',
    },
    STC: {
        both_options: 'Threads=1 Hash=64',
        both_time_control: '8.0+0.08',
        workload_size: 32,
    },
    '回帰確認': {
        both_options: 'Threads=1 Hash=64',
        test_bounds: '[-4.00, 0.00]',
    },
    '固定局数': {
        both_options: 'Threads=1 Hash=64',
        test_max_games: 2000,
    },
};


test('名前付きプリセットはブランチと局面集を変更しない', () => {
    const context = loadScript(presets);
    const settings = context.settings_for_preset('engine', 'STC', 'TEST');

    assert.equal(settings.base_branch, undefined);
    assert.equal(settings.book_name, undefined);
    assert.equal(settings.win_adj, undefined);
    assert.equal(settings.both_time_control, '8.0+0.08');
    assert.equal(settings.workload_size, 32);
});


test('検定方式の切替項目はプリセット間で既定値へ戻る', () => {
    const context = loadScript(presets);
    const settings = context.settings_for_preset('engine', 'STC', 'TEST');

    assert.equal(settings.test_bounds, '[0.00, 4.00]');
    assert.equal(settings.test_confidence, '[0.10, 0.05]');
    assert.equal(settings.test_max_games, undefined);
});


test('初期表示用defaultだけは全項目を適用する', () => {
    const context = loadScript(presets);
    const settings = context.settings_for_preset('engine', 'default', 'TEST');

    assert.equal(settings.base_branch, 'master');
    assert.equal(settings.book_name, 'default.epd');
    assert.equal(settings.win_adj, 'movecount=3 score=2000');
});
