var branch_cache            = new Map();
var branch_request_versions = {};
var branch_refresh_timers   = {};
var pending_branch_values   = {};

function set_branch_placeholder(target, text, busy, title) {

    const select = document.getElementById(target + '_branch');
    if (select == null)
        return;

    while (select.length)
        select.remove(0);

    const option    = document.createElement('option');
    option.text     = text;
    option.value    = '';
    option.disabled = true;
    option.selected = true;
    select.add(option);

    select.dataset.loaded = 'false';
    select.title = title || '';
    select.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function accept_branch_selection(target) {
    delete pending_branch_values[target + '_branch'];
}

function initialize_branch_selector(target, branch) {
    if (branch)
        pending_branch_values[target + '_branch'] = branch;
    set_branch_placeholder(target, 'GitHubから取得中...', true);
    refresh_branch_options(target);
}

function schedule_branch_refresh(target) {

    const field_id = target + '_branch';
    const select   = document.getElementById(field_id);

    if (select != null && select.dataset.loaded === 'true' && select.value)
        pending_branch_values[field_id] = select.value;

    branch_request_versions[target] = (branch_request_versions[target] || 0) + 1;
    set_branch_placeholder(target, 'リポジトリ入力待ち...', true);
    clearTimeout(branch_refresh_timers[target]);
    branch_refresh_timers[target] = setTimeout(
        function() { refresh_branch_options(target); }, 400);
}

async function load_branch_data(engine, repo, force) {

    const key = engine + '\n' + repo;
    if (force)
        branch_cache.delete(key);

    if (!branch_cache.has(key)) {
        const query = new URLSearchParams({ 'engine' : engine, 'repo' : repo });
        const promise = fetch('/api/branches/?' + query.toString()).then(async function(response) {
            let data;
            try {
                data = await response.json();
            } catch (error) {
                throw new Error('サーバーから不正な応答を受信しました');
            }

            if (!response.ok)
                throw new Error(data.error || 'ブランチ一覧を取得できませんでした');

            if (!Array.isArray(data.branches) || typeof data.default_branch !== 'string')
                throw new Error('ブランチ一覧の形式が不正です');

            return data;
        });

        branch_cache.set(key, promise);
        promise.catch(function() {
            if (branch_cache.get(key) === promise)
                branch_cache.delete(key);
        });
    }

    return branch_cache.get(key);
}

async function refresh_branch_options(target, force) {

    const select = document.getElementById(target + '_branch');
    if (select == null)
        return;

    const engine = document.getElementById(target + '_engine').value;
    const repo   = document.getElementById(target + '_repo').value.trim().replace(/\/$/, '');
    const field_id = target + '_branch';
    const desired = pending_branch_values[field_id]
        || (select.dataset.loaded === 'true' ? select.value : '');
    const version = (branch_request_versions[target] || 0) + 1;
    branch_request_versions[target] = version;

    set_branch_placeholder(target, 'GitHubから取得中...', true);

    try {
        const data = await load_branch_data(engine, repo, Boolean(force));
        if (version !== branch_request_versions[target])
            return;

        while (select.length)
            select.remove(0);

        for (const branch of data.branches) {
            const option = document.createElement('option');
            option.text  = branch;
            option.value = branch;
            select.add(option);
        }

        const selected = desired || data.default_branch;
        if (data.branches.includes(selected)) {
            select.value = selected;
            delete pending_branch_values[field_id];
        } else if (desired) {
            const option    = document.createElement('option');
            option.text     = '指定ブランチが見つかりません: ' + desired;
            option.value    = '';
            option.disabled = true;
            option.selected = true;
            select.insertBefore(option, select.firstChild);
        } else if (data.branches.length) {
            select.selectedIndex = 0;
        } else {
            set_branch_placeholder(target, 'ブランチがありません', false);
            return;
        }

        select.dataset.loaded = 'true';
        select.title = '';
        select.setAttribute('aria-busy', 'false');

    } catch (error) {
        if (version !== branch_request_versions[target])
            return;
        set_branch_placeholder(
            target, '取得失敗 - 再取得してください', false,
            error instanceof Error ? error.message : String(error));
    }
}
