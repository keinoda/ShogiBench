(function () {
    'use strict';

    const NS = 'http://www.w3.org/2000/svg';
    const COLORS = ['#5DADE2', '#F5B041', '#58D68D', '#EC7063',
                    '#AF7AC5', '#48C9B0', '#F4D03F', '#AAB7B8'];

    function element(name, attributes, text) {
        const node = document.createElementNS(NS, name);
        Object.entries(attributes || {}).forEach(([key, value]) => node.setAttribute(key, value));
        if (text !== undefined)
            node.textContent = text;
        return node;
    }

    function div(className, text) {
        const node = document.createElement('div');
        node.className = className;
        if (text !== undefined)
            node.textContent = text;
        return node;
    }

    function polylinePoints(points, xScale, yScale) {
        return points.map(point =>
            `${xScale(point[0]).toFixed(2)},${yScale(point[1]).toFixed(2)}`
        ).join(' ');
    }

    function makeChart(container, title, description, series, xMax, yMax, yUnit) {
        const width = 960, height = 260;
        const plot = { left: 72, right: 24, top: 22, bottom: 42 };
        const innerWidth = width - plot.left - plot.right;
        const innerHeight = height - plot.top - plot.bottom;
        const safeXMax = Math.max(1, xMax);
        const safeYMax = Math.max(0.001, yMax);
        const xScale = value => plot.left + innerWidth * value / safeXMax;
        const yScale = value => plot.top + innerHeight * (safeYMax - value) / (2 * safeYMax);

        container.appendChild(div('spsa-chart-title', title));
        const svg = element('svg', {
            class: 'spsa-chart-svg', viewBox: `0 0 ${width} ${height}`,
            role: 'img', 'aria-label': description,
        });
        svg.appendChild(element('title', {}, title));
        svg.appendChild(element('desc', {}, description));

        for (let index = 0; index <= 4; index++) {
            const value = safeYMax - (2 * safeYMax * index / 4);
            const y = yScale(value);
            svg.appendChild(element('line', { class: value === 0 ? 'zero-line' : 'grid',
                x1: plot.left, y1: y, x2: width - plot.right, y2: y }));
            svg.appendChild(element('text', { x: plot.left - 9, y: y + 4, 'text-anchor': 'end' },
                `${value.toFixed(value >= 10 || value <= -10 ? 1 : 2)}${yUnit}`));
        }

        for (let index = 0; index <= 4; index++) {
            const value = safeXMax * index / 4;
            const x = xScale(value);
            svg.appendChild(element('line', { class: 'grid', x1: x, y1: plot.top,
                x2: x, y2: height - plot.bottom }));
            svg.appendChild(element('text', { x: x, y: height - 16, 'text-anchor': 'middle' },
                Math.round(value).toString()));
        }
        svg.appendChild(element('text', { x: width - plot.right, y: height - 3,
            'text-anchor': 'end' }, 'batch'));

        series.forEach(item => {
            if (!item.points.length)
                return;
            svg.appendChild(element('polyline', {
                class: `series ${item.className || ''}`,
                points: polylinePoints(item.points, xScale, yScale),
                stroke: item.color,
            }));
            if (item.markers) {
                item.points.forEach(point => svg.appendChild(element('circle', {
                    class: 'series-point', cx: xScale(point[0]), cy: yScale(point[1]),
                    r: 2.2, fill: item.color,
                })));
            }
        });
        container.appendChild(svg);
    }

    function renderScoreChart(data) {
        const container = document.getElementById('spsa-score-chart');
        if (!container || !data.stats.length)
            return;

        const raw = data.stats.map(row => [row[0], 100 * row[2] / Math.max(1, row[1])]);
        const rolling = raw.map((point, index) => {
            const window = raw.slice(Math.max(0, index - data.window + 1), index + 1);
            return [point[0], window.reduce((sum, value) => sum + value[1], 0) / window.length];
        });
        const yMax = Math.max(1, ...raw.map(point => Math.abs(point[1])));
        const xMax = Math.max(data.total_batches, ...raw.map(point => point[0]));

        makeChart(container, '+ / − 摂動のスコア差',
            '各batchのraw_resultをペア数で正規化した値と、16 batch移動平均。0付近を上下するかを確認する。',
            [
                { points: raw, color: '#949BA4', className: 'raw-series' },
                { points: rolling, color: COLORS[0] },
            ], xMax, yMax, '%');

        const latestRaw = raw[raw.length - 1][1];
        const latestRolling = rolling[rolling.length - 1][1];
        container.appendChild(div('spsa-chart-summary',
            `直近 ${latestRaw >= 0 ? '+' : ''}${latestRaw.toFixed(1)}% / ` +
            `${data.window} batch 移動平均 ${latestRolling >= 0 ? '+' : ''}${latestRolling.toFixed(2)}%`));
    }

    function renderParameterChart(data) {
        const container = document.getElementById('spsa-parameter-chart');
        if (!container || data.values.length < 2)
            return;

        const normalizedRows = data.values.map(row => [row[0], row[1].map((value, index) => {
            const definition = data.definitions[index];
            const range = definition[2] - definition[1];
            return range ? 100 * (value - definition[0]) / range : 0;
        })]);
        const latest = normalizedRows[normalizedRows.length - 1][1];
        const selected = latest.map((value, index) => ({ index, value }))
            .filter(item => data.definitions[item.index][3])
            .sort((left, right) => Math.abs(right.value) - Math.abs(left.value))
            .slice(0, 8);
        if (!selected.length)
            return;

        const series = selected.map((item, colorIndex) => ({
            name: data.names[item.index],
            value: item.value,
            color: COLORS[colorIndex],
            points: normalizedRows.map(row => [row[0], row[1][item.index]]),
            markers: true,
        }));
        const yMax = Math.max(0.5, ...series.flatMap(item => item.points.map(point => Math.abs(point[1]))));
        const xMax = Math.max(data.total_batches, ...normalizedRows.map(row => row[0]));

        makeChart(container, '初期値から最も動いたパラメータ',
            '可動範囲に対する初期値からの符号付き変化率。正は増加、負は減少を示す。',
            series, xMax, yMax, '%');

        const legend = div('spsa-chart-legend');
        series.forEach(item => {
            const label = document.createElement('span');
            label.style.setProperty('--series-color', item.color);
            label.textContent = `${item.name} ${item.value >= 0 ? '+' : ''}${item.value.toFixed(2)}%`;
            legend.appendChild(label);
        });
        container.appendChild(legend);

        const last = normalizedRows[normalizedRows.length - 1][1];
        const active = last.filter((value, index) => data.definitions[index][3]);
        const rms = Math.sqrt(active.reduce((sum, value) => sum + value * value, 0) /
                              Math.max(1, active.length));
        const stepRms = normalizedRows.slice(1).map((row, rowIndex) => {
            const previous = normalizedRows[rowIndex][1];
            const deltas = row[1].map((value, index) => value - previous[index])
                .filter((value, index) => data.definitions[index][3]);
            return Math.sqrt(deltas.reduce((sum, value) => sum + value * value, 0) /
                             Math.max(1, deltas.length));
        });
        container.appendChild(div('spsa-chart-summary',
            `初期値からのRMS距離 ${rms.toFixed(2)}% / ` +
            `直近step RMS ${stepRms[stepRms.length - 1].toFixed(2)}% / ` +
            `最大step RMS ${Math.max(...stepRms).toFixed(2)}%`));
    }

    document.addEventListener('DOMContentLoaded', function () {
        const source = document.getElementById('spsa-trajectory-data');
        if (!source)
            return;
        try {
            const data = JSON.parse(source.textContent);
            renderScoreChart(data);
            renderParameterChart(data);
        } catch (error) {
            console.error('SPSA trajectory chart could not be rendered:', error);
        }
    });
}());
