
var config   = JSON.parse(document.getElementById('json-config'  ).textContent);
var networks = JSON.parse(document.getElementById('json-networks').textContent);
var repos    = JSON.parse(document.getElementById('json-repos'   ).textContent);

// Static json variants merged with user-defined ones from /builds/
var build_variants_el = document.getElementById('json-build-variants');
var build_variants    = build_variants_el ? JSON.parse(build_variants_el.textContent) : {};

// .tune kits registered on /tunekits/ (SPSA tuning pages only)
var tune_kits_el = document.getElementById('json-tune-kits');
var tune_kits    = (tune_kits_el && JSON.parse(tune_kits_el.textContent)) || [];

function create_network_options(field_id, engine) {

    var has_default     = false;
    var network_options = document.getElementById(field_id);

    // Delete all existing Networks
    while (network_options.length)
        network_options.remove(0);

    // Add each Network that matches the given engine
    for (const network of networks) {

        if (network.engine !== engine)
            continue;

        var opt      = document.createElement('option');
        opt.text     = network.name;
        opt.value    = network.sha256;
        opt.selected = network.default;
        network_options.add(opt)

        has_default = has_default || network.default;
    }

    { // Add a None option and set it to default if there was not one yet
        var opt       = document.createElement('option');
        opt.text      = 'None';
        opt.value     = '';
        opt.selected  = !has_default;
        network_options.add(opt);
    }
}

function create_build_options(field_id, engine) {

    var build_options = document.getElementById(field_id);

    // TUNE pages only have a dev_build selector
    if (build_options == null)
        return;

    // Delete all existing Variants
    while (build_options.length)
        build_options.remove(0);

    // Add each Build Variant defined for the engine
    const variants = build_variants[engine]
        || (config.engines[engine].build || {}).variants
        || { 'default' : '' };
    for (const name in variants) {

        var opt      = document.createElement('option');
        opt.text     = name;
        opt.value    = name;
        opt.selected = name === 'default';
        opt.title    = variants[name];
        build_options.add(opt);
    }
}

function create_tune_kit_options(field_id, engine) {

    var kit_options = document.getElementById(field_id);

    // Only SPSA tuning pages have the .tune kit selector
    if (kit_options == null)
        return;

    while (kit_options.length)
        kit_options.remove(0);

    { // 'なし' = engine already exposes the parameters as USI options
        var opt      = document.createElement('option');
        opt.text     = 'なし';
        opt.value    = '';
        opt.selected = true;
        kit_options.add(opt);
    }

    for (const kit of tune_kits) {

        if (kit.engine !== engine)
            continue;

        var opt   = document.createElement('option');
        opt.text  = kit.name;
        opt.value = kit.id;
        kit_options.add(opt);
    }
}

function apply_tune_kit() {

    // Fill the SPSA inputs with the kit's .params, and force mapping off
    // (kit parameter names ARE the TUNE build's USI option names)

    const selection = document.getElementById('spsa_tune_kit');
    const kit_id    = selection.options[selection.selectedIndex].value;

    if (kit_id === '')
        return;

    for (const kit of tune_kits) {
        if (String(kit.id) === kit_id) {
            document.getElementById('spsa_inputs').value  = kit.params_text;
            document.getElementById('spsa_mapping').value = 'NONE';
            break;
        }
    }
}

function create_preset_buttons(engine, workload_type) {

    // Clear out all of the existing buttons
    var button_div = document.getElementById('test-mode-buttons');
    while (button_div.hasChildNodes())
        button_div.removeChild(button_div.lastChild);

    const presets = workload_type == 'TEST'    ? config.engines[engine].test_presets
                  : workload_type == 'TUNE'    ? config.engines[engine].tune_presets
                  : workload_type == 'DATAGEN' ? config.engines[engine].datagen_presets : {};

    var index = 0;
    for (let mode in presets) {

        // Don't include the global defaults
        if (mode == 'default')
            continue;

        // Create a new button for the test mode
        var btn       = document.createElement('button')
        btn.innerHTML = mode;
        btn.onclick   = function() { apply_preset(mode, workload_type); };

        // Apply all of our CSS bootstrapping
        btn.classList.add('anchorbutton');
        btn.classList.add('btn-preset');
        btn.classList.add('mt-1');
        btn.classList.add('w-100');

        // Put the button in a div, so we can handle padding
        var div = document.createElement('div')
        div.appendChild(btn)
        div.classList.add('col-half');

        // Left pad everything but the first
        if ((index % 4) != 0)
            div.classList.add('pl-half');

        // Right pad everything but the last
        if ((index % 4) != 3)
            div.classList.add('pr-half');

        button_div.append(div);
        index++;
    }
}


function get_dev_engine() {
    const selection = document.getElementById('dev_engine');
    return selection.options[selection.selectedIndex].value;
}

function get_base_engine() {
    const selection = document.getElementById('base_engine');
    return selection.options[selection.selectedIndex].value;
}

function get_presets(engine, preset, workload_type) {
    return workload_type == 'TEST'    ? config.engines[engine].test_presets[preset]
         : workload_type == 'TUNE'    ? config.engines[engine].tune_presets[preset]
         : workload_type == 'DATAGEN' ? config.engines[engine].datagen_presets[preset] : {};
}


function add_defaults_to_preset(engine, preset, workload_type) {

    const default_settings = get_presets(engine, 'default', workload_type);
    const preset_settings  = get_presets(engine, preset, workload_type);

    let settings = {}

    for (const key in default_settings)
        settings[key] = default_settings[key];

    for (const key in preset_settings)
        settings[key] = preset_settings[key];

    return settings;
}


function preset_managed_options(engine, workload_type) {

    const presets = workload_type == 'TEST'    ? config.engines[engine].test_presets
                  : workload_type == 'TUNE'    ? config.engines[engine].tune_presets
                  : workload_type == 'DATAGEN' ? config.engines[engine].datagen_presets : {};

    const managed = new Set();
    for (const name in presets) {
        if (name == 'default')
            continue;
        for (const option in presets[name])
            managed.add(option);
    }

    // 検定方式の3項目は連動する。固定局数からSPRTへ戻すときに、
    // boundsだけでなくconfidenceも既定値へ戻す必要がある。
    const test_mode_options = ['test_bounds', 'test_confidence', 'test_max_games'];
    if (test_mode_options.some(option => managed.has(option)))
        for (const option of test_mode_options)
            managed.add(option);

    return managed;
}


function settings_for_preset(engine, preset, workload_type) {

    const selected = get_presets(engine, preset, workload_type) || {};

    // 明示的なdefault適用（初期表示・エンジン変更）は全項目を対象にする。
    if (preset == 'default')
        return selected;

    // ボタン押下時は、いずれかの名前付きプリセットが管理する項目だけを
    // defaultへ戻してから選択値を重ねる。ブランチや局面集は保持する。
    const defaults = get_presets(engine, 'default', workload_type) || {};
    const settings = {};
    for (const option of preset_managed_options(engine, workload_type))
        if (defaults.hasOwnProperty(option))
            settings[option] = defaults[option];

    for (const option in selected)
        settings[option] = selected[option];

    return settings;
}


function option_tokens(options) {
    return options.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
}


function option_name(token) {
    const separator = token.indexOf('=');
    return (separator == -1 ? token : token.slice(0, separator)).toLowerCase();
}


function merge_required_options(options, required_options) {

    const required = option_tokens(required_options || '');
    const required_names = new Set(required.map(option_name));
    const optional = option_tokens(options || '').filter(
        token => !required_names.has(option_name(token))
    );
    return optional.concat(required).join(' ');
}


function apply_required_test_options(workload_type) {

    if (workload_type != 'TEST')
        return;

    for (const target of ['dev', 'base']) {
        const engine_field = document.getElementById(target + '_engine');
        const options_field = document.getElementById(target + '_options');
        if (!engine_field || !options_field)
            continue;

        const required = config.engines[engine_field.value].test_required_options || '';
        options_field.value = merge_required_options(options_field.value, required);
    }
}

function set_engine(engine, target) {

    document.getElementById(target + '_engine').value = engine;
    document.getElementById(target + '_repo'  ).value = repos[engine] || config.engines[engine].source

    delete pending_branch_values[target + '_branch'];
    set_branch_placeholder(target, 'GitHubから取得中...', true);
    refresh_branch_options(target);

    create_network_options(target + '_network', engine);
    create_build_options(target + '_build', engine);

    if (target == 'dev')
        create_tune_kit_options('spsa_tune_kit', engine);
}

function set_option(option_name, option_value) {

    const element = document.getElementById(option_name);

    if (element == null)
        console.log(option_name + ' was not found.');

    else if (element.tagName.toLowerCase() != 'select') {

        element.value = option_value;

        if (option_name == 'test_max_games') {
            document.getElementById('test_mode').value = "GAMES";
            document.getElementById('test_bounds').value = 'N/A';
            document.getElementById('test_confidence').value = 'N/A';
        }

        if (option_name == 'test_bounds' || option_name == 'test_confidence') {
            document.getElementById('test_mode').value = "SPRT";
            document.getElementById('test_max_games').value = 'N/A';
        }
    }

    else {
        var matched = false;
        for (let i = 0; i < element.options.length; i++)
            if (element.options[i].text === option_value || element.options[i].value === option_value) {
                element.value = element.options[i].value;
                matched = true;
            }

        if (option_name.endsWith('_branch')) {
            if (matched) {
                delete pending_branch_values[option_name];
            } else {
                pending_branch_values[option_name] = option_value;

                var option      = document.createElement('option');
                option.text     = '指定ブランチが見つかりません: ' + option_value;
                option.value    = '';
                option.disabled = true;
                option.selected = true;
                element.insertBefore(option, element.firstChild);
            }
        }
    }
}

function retain_specific_options(engine, preset, workload_type) {

    // This is not applicable for self-play
    if (get_dev_engine() == get_base_engine())
        return;

    // Extract the Threads and Hash settings from the Dev Options

    const dev_options   = document.getElementById('dev_options').value;

    const threads_match = dev_options.match(/\bThreads\s*=\s*(\d+)\b/);
    const hash_match    = dev_options.match(/\bHash\s*=\s*(\d+)\b/);

    const dev_threads   = threads_match ? threads_match[1] : null;
    const dev_hash      = hash_match    ? hash_match[1]    : null;

    // From the base options, replace the Threads= and Hash=

    const settings = add_defaults_to_preset(engine, preset, workload_type);

    let base_options = settings['base_options'] || settings['both_options'];

    base_options = base_options.replace(/\bThreads\s*=\s*\d+\b/g, 'Threads=' + dev_threads);
    base_options = base_options.replace(/\bHash\s*=\s*\d+\b/g, 'Hash=' + dev_hash);

    set_option('base_options', base_options);

}


function apply_preset(preset, workload_type) {

    const settings = settings_for_preset(get_dev_engine(), preset, workload_type);

    for (const option in settings) {

        if (!settings.hasOwnProperty(option))
            continue;

        else if (!option.startsWith('both_'))
            set_option(option, settings[option]);

        else {
            set_option(option.replace('both_', 'dev_'), settings[option]);

            if (workload_type == 'TEST' || workload_type == "DATAGEN")
                set_option(option.replace('both_', 'base_'), settings[option]);
        }
    }

    // For cross-engine tests, keep the original Hash/Threads, but
    // add any other settings that might be specific to the engine
    if (workload_type == 'TEST' || workload_type == "DATAGEN") {
        try {
            retain_specific_options(get_base_engine(), preset, workload_type);
        } catch (error) {}
    }

    apply_required_test_options(workload_type);
}

function change_engine(engine, target, workload_type) {

    // Profiles may still point at a renamed or removed engine; falling
    // back keeps the page initializing instead of dying on a TypeError
    if (!(engine in config.engines))
        engine = Object.keys(config.engines)[0];

    set_engine(engine, target);

    if (target == 'dev')
        create_preset_buttons(engine, workload_type);

    if (target == 'dev' && (workload_type == 'TEST' || workload_type == 'DATAGEN'))
        set_engine(engine, 'base');

    set_option('scale_nps', config.engines[engine].nps);
    set_option('scale_method', workload_type == 'TUNE' ? 'DEV' : 'BASE');

    // 初期表示とエンジン変更時だけ、ブランチ・局面集を含む全defaultを適用する。
    apply_preset('default', workload_type);
    if (get_presets(get_dev_engine(), 'STC', workload_type))
        apply_preset('STC', workload_type);
}

function set_test_type() {

    // When swapping from SPRT -> FIXED, we disable test_bounds and test_confidence
    // When swapping from FIXED -> SPRT, we disable test_max_games
    //
    // Attempt to fill SPRT fields using default settings, then STC settings.
    // Attempt to fill FIXED fields using default settings, then just use 40,000

    var selectA  = document.getElementById('test_mode');
    var mode     = selectA.options[selectA.selectedIndex].value;

    var selectB  = document.getElementById('dev_engine');
    var engine   = selectB.options[selectB.selectedIndex].value;

    var base = get_presets(engine, 'default', 'TEST');
    var stc  = get_presets(engine, 'STC', 'TEST');

    if (!stc) // If there are no STC settings, re-use the defaults
        stc = base;

    if (mode == 'SPRT') {
        document.getElementById('test_bounds'    ).value = base.test_bounds || stc.test_bounds;
        document.getElementById('test_confidence').value = base.test_confidence || stc.test_confidence;
        document.getElementById('test_max_games' ).value = 'N/A';
    }

    if (mode == 'GAMES') {
        document.getElementById('test_bounds'    ).value = 'N/A';
        document.getElementById('test_confidence').value = 'N/A';
        document.getElementById('test_max_games' ).value = base.test_max_games || stc.test_max_games || 40000;
    }
}
