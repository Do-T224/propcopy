/**
 * Insights charts — renders all analytics sub-tab visualizations.
 * Uses Chart.js (already loaded globally).
 */

const InsightCharts = {
    _charts: {},

    /** Destroy a chart by key if it exists */
    _destroy(key) {
        if (this._charts[key]) {
            this._charts[key].destroy();
            delete this._charts[key];
        }
    },

    /** Check if a canvas element is visible and has dimensions */
    _canvasReady(id) {
        const el = document.getElementById(id);
        if (!el) { console.warn('Canvas not found:', id); return false; }
        if (el.offsetWidth === 0 || el.offsetHeight === 0) {
            console.warn('Canvas has zero dimensions:', id, el.offsetWidth, el.offsetHeight);
            return false;
        }
        return true;
    },

    /** Destroy all charts */
    destroyAll() {
        for (const key of Object.keys(this._charts)) {
            this._charts[key].destroy();
        }
        this._charts = {};
    },

    // ── Overview KPIs are rendered as HTML, not charts ──

    // ══════════════════════════════════════════════════════
    // Feature 1: Entry Timing Aggregate Bar Chart
    // ══════════════════════════════════════════════════════
    renderEntryTiming(canvasId, aggregate) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !aggregate) return;

        const labels = ['-5m', '-1m', '-30s', '+30s', '+1m', '+5m'];
        const data = labels.map(l => {
            const d = aggregate[l];
            return d ? d.avg_improvement : 0;
        });
        const colors = data.map(v => v > 0 ? 'rgba(16, 185, 129, 0.8)' : 'rgba(239, 68, 68, 0.8)');

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Avg Price Improvement',
                    data,
                    backgroundColor: colors,
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'Entry Timing: Avg Improvement vs Actual', color: '#9ca3af', font: { size: 13 } },
                    tooltip: {
                        callbacks: {
                            afterLabel(ctx) {
                                const lbl = labels[ctx.dataIndex];
                                const d = aggregate[lbl];
                                if (!d) return '';
                                return `Better: ${d.better_count}/${d.total} | Worse: ${d.worse_count}/${d.total}`;
                            }
                        }
                    }
                },
                scales: {
                    x: { ticks: { color: '#9ca3af' }, grid: { display: false } },
                    y: {
                        title: { display: true, text: 'Price Improvement', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Exit Timing: What if trader held longer?
    // ══════════════════════════════════════════════════════
    renderExitTiming(canvasId, aggregate) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !aggregate) return;

        const labels = ['30s', '1m', '5m', '15m', '30m', '1h'];
        const profits = labels.map(l => aggregate[l]?.avg_potential_profit || 0);
        const losses = labels.map(l => -(aggregate[l]?.avg_potential_loss || 0));

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Avg Potential Profit',
                        data: profits,
                        backgroundColor: 'rgba(16, 185, 129, 0.8)',
                    },
                    {
                        label: 'Avg Potential Loss',
                        data: losses,
                        backgroundColor: 'rgba(239, 68, 68, 0.8)',
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#9ca3af' } },
                    title: { display: true, text: 'Exit Timing: Potential P/L If Held Longer', color: '#9ca3af', font: { size: 13 } },
                    tooltip: {
                        callbacks: {
                            afterLabel(ctx) {
                                const lbl = labels[ctx.dataIndex];
                                const d = aggregate[lbl];
                                if (!d) return '';
                                return `Based on ${d.total} trades`;
                            }
                        }
                    }
                },
                scales: {
                    x: { ticks: { color: '#9ca3af' }, grid: { display: false } },
                    y: {
                        title: { display: true, text: 'Price Movement', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 2: Exit Efficiency Scatter
    // ══════════════════════════════════════════════════════
    renderExitEfficiency(canvasId, data) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !data || !data.trades) return;

        const points = data.trades
            .filter(t => t.mfe > 0)
            .map(t => ({ x: t.mfe, y: t.pnl, ticket: t.ticket }));

        if (!points.length) return;

        const maxVal = Math.max(...points.map(p => Math.max(Math.abs(p.x), Math.abs(p.y))));

        this._charts[canvasId] = new Chart(ctx, {
            type: 'scatter',
            data: {
                datasets: [
                    {
                        label: 'Trades',
                        data: points,
                        backgroundColor: points.map(p => p.y >= 0 ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)'),
                        pointRadius: 5,
                        pointHoverRadius: 7,
                    },
                    {
                        // Perfect exit diagonal line
                        label: 'Perfect Exit',
                        data: [{ x: 0, y: 0 }, { x: maxVal, y: maxVal }],
                        type: 'line',
                        borderColor: 'rgba(107,114,128,0.4)',
                        borderDash: [5, 5],
                        borderWidth: 1,
                        pointRadius: 0,
                        fill: false,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: `Exit Efficiency (avg ${(data.avg_efficiency * 100).toFixed(0)}%)`, color: '#9ca3af', font: { size: 13 } },
                    tooltip: {
                        callbacks: {
                            label(ctx) {
                                if (ctx.datasetIndex !== 0) return '';
                                const p = ctx.raw;
                                const eff = p.x > 0 ? ((p.y / p.x) * 100).toFixed(0) : 'N/A';
                                return `#${p.ticket}: MFE=$${p.x.toFixed(2)}, PnL=$${p.y.toFixed(2)} (${eff}%)`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        title: { display: true, text: 'MFE ($)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                    y: {
                        title: { display: true, text: 'Actual PnL ($)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 5: Duration vs Outcome Scatter
    // ══════════════════════════════════════════════════════
    renderDurationOutcome(canvasId, data) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !data || !data.trades) return;

        const points = data.trades.map(t => ({
            x: t.duration / 60, // convert to minutes
            y: t.pnl,
            ticket: t.ticket,
        }));

        if (!points.length) return;

        const medDur = data.median_duration / 60;
        const q = data.quadrants;

        this._charts[canvasId] = new Chart(ctx, {
            type: 'scatter',
            data: {
                datasets: [{
                    label: 'Trades',
                    data: points,
                    backgroundColor: points.map(p => p.y >= 0 ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)'),
                    pointRadius: 5,
                    pointHoverRadius: 7,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: {
                        display: true,
                        text: `Duration vs PnL | QW:${q.quick_win} QL:${q.quick_loss} SW:${q.slow_win} SL:${q.slow_loss}`,
                        color: '#9ca3af', font: { size: 13 },
                    },
                    tooltip: {
                        callbacks: {
                            label(ctx) {
                                const p = ctx.raw;
                                return `#${p.ticket}: ${p.x.toFixed(1)}min, $${p.y.toFixed(2)}`;
                            }
                        }
                    },
                    annotation: undefined, // We'll draw quadrant lines manually via plugin
                },
                scales: {
                    x: {
                        title: { display: true, text: 'Duration (min)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                    y: {
                        title: { display: true, text: 'PnL ($)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
            plugins: [{
                // Draw zero line and median duration line
                afterDraw(chart) {
                    const { ctx, scales } = chart;
                    const xScale = scales.x;
                    const yScale = scales.y;

                    // Horizontal zero line
                    const y0 = yScale.getPixelForValue(0);
                    ctx.save();
                    ctx.strokeStyle = 'rgba(107,114,128,0.4)';
                    ctx.setLineDash([4, 4]);
                    ctx.beginPath();
                    ctx.moveTo(xScale.left, y0);
                    ctx.lineTo(xScale.right, y0);
                    ctx.stroke();

                    // Vertical median duration line
                    const xMed = xScale.getPixelForValue(medDur);
                    ctx.beginPath();
                    ctx.moveTo(xMed, yScale.top);
                    ctx.lineTo(xMed, yScale.bottom);
                    ctx.stroke();
                    ctx.restore();
                }
            }],
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 3: SL/TP Exit Distribution (Donut)
    // ══════════════════════════════════════════════════════
    renderExitDistribution(canvasId, exitDist) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !exitDist) return;

        const labels = ['SL Hit', 'TP Hit', 'Manual'];
        const values = [exitDist.sl || 0, exitDist.tp || 0, exitDist.manual || 0];
        const total = values.reduce((a, b) => a + b, 0);
        if (!total) return;

        this._charts[canvasId] = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels,
                datasets: [{
                    data: values,
                    backgroundColor: ['rgba(239,68,68,0.8)', 'rgba(16,185,129,0.8)', 'rgba(59,130,246,0.8)'],
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: 'bottom', labels: { color: '#9ca3af', padding: 12 } },
                    title: { display: true, text: 'Exit Type Distribution', color: '#9ca3af', font: { size: 13 } },
                    tooltip: {
                        callbacks: {
                            label(ctx) {
                                const pct = ((ctx.raw / total) * 100).toFixed(1);
                                return `${ctx.label}: ${ctx.raw} (${pct}%)`;
                            }
                        }
                    }
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 7: Planned vs Realized R:R — Line (all trades)
    // ══════════════════════════════════════════════════════
    // Potential R:R line colors per interval
    _potentialColors: {
        // Exit: if held longer
        'exit_1m':  { border: 'rgba(251,191,36,0.7)',  label: 'Held +1m' },
        'exit_5m':  { border: 'rgba(249,115,22,0.7)',  label: 'Held +5m' },
        'exit_15m': { border: 'rgba(236,72,153,0.7)',  label: 'Held +15m' },
        'exit_30m': { border: 'rgba(139,92,246,0.7)',  label: 'Held +30m' },
        // Entry: if entered earlier/later
        'entry_-5m':  { border: 'rgba(34,211,238,0.7)',  label: 'Entry -5m' },
        'entry_-1m':  { border: 'rgba(56,189,248,0.7)',  label: 'Entry -1m' },
        'entry_-30s': { border: 'rgba(129,140,248,0.7)', label: 'Entry -30s' },
        'entry_+30s': { border: 'rgba(167,139,250,0.7)', label: 'Entry +30s' },
        'entry_+1m':  { border: 'rgba(192,132,252,0.7)', label: 'Entry +1m' },
        'entry_+5m':  { border: 'rgba(232,121,249,0.7)', label: 'Entry +5m' },
    },

    renderRiskRewardLine(canvasId, data, selectedIntervals) {
        this._destroy(canvasId);
        if (!this._canvasReady(canvasId)) return;
        const ctx = document.getElementById(canvasId);
        if (!data || !data.trades || !data.trades.length) return;

        const trades = data.trades;
        const intervals = selectedIntervals || [];
        const labels = trades.map(t => {
            if (!t.open_time) return '';
            const d = new Date(t.open_time * 1000);
            const dd = String(d.getDate()).padStart(2, '0');
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            return `${dd}/${mm}`;
        });

        const datasets = [
            {
                label: 'Planned R:R',
                data: trades.map(t => t.planned_rr),
                borderColor: 'rgba(59,130,246,0.9)',
                fill: false,
                tension: 0.3,
                pointRadius: 2,
                pointHoverRadius: 5,
                borderWidth: 2,
            },
            {
                label: 'Realized R:R',
                data: trades.map(t => t.realized_rr),
                borderColor: 'rgba(16,185,129,0.9)',
                fill: false,
                tension: 0.3,
                pointRadius: 2,
                pointHoverRadius: 5,
                borderWidth: 2,
            },
        ];

        // Add potential R:R lines for selected intervals
        for (const interval of intervals) {
            const color = this._potentialColors[interval];
            if (!color) continue;

            const hasData = trades.some(t => t.potential_rr?.[interval] !== undefined);
            if (!hasData) continue;

            datasets.push({
                label: color.label,
                data: trades.map(t => t.potential_rr?.[interval] ?? null),
                borderColor: color.border,
                fill: false,
                tension: 0.3,
                pointRadius: 2,
                pointHoverRadius: 5,
                borderWidth: 1.5,
                borderDash: [4, 3],
                spanGaps: true,
            });
        }

        this._charts[canvasId] = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#9ca3af', font: { size: 10 } } },
                    title: {
                        display: true,
                        text: `R:R Over Time — Planned avg ${data.avg_planned} | Realized avg ${data.avg_realized}`,
                        color: '#9ca3af', font: { size: 13 },
                    },
                    tooltip: {
                        callbacks: {
                            title(items) {
                                const idx = items[0]?.dataIndex;
                                if (idx === undefined) return '';
                                const t = trades[idx];
                                if (!t.open_time) return '#' + t.ticket;
                                const d = new Date(t.open_time * 1000);
                                const dd = String(d.getDate()).padStart(2, '0');
                                const mm = String(d.getMonth() + 1).padStart(2, '0');
                                const hh = String(d.getHours()).padStart(2, '0');
                                const mi = String(d.getMinutes()).padStart(2, '0');
                                const ss = String(d.getSeconds()).padStart(2, '0');
                                return `#${t.ticket} — ${dd}/${mm} ${hh}:${mi}:${ss}`;
                            }
                        }
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#9ca3af', maxTicksLimit: 15, font: { size: 9 } },
                        grid: { color: 'rgba(107,114,128,0.05)' },
                    },
                    y: {
                        title: { display: true, text: 'Risk:Reward', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 7: Planned vs Realized R:R — Bar (last 10)
    // ══════════════════════════════════════════════════════
    renderRiskRewardBar(canvasId, data) {
        this._destroy(canvasId);
        if (!this._canvasReady(canvasId)) return;
        const ctx = document.getElementById(canvasId);
        if (!data || !data.trades || !data.trades.length) return;

        const trades = data.trades.slice(-10);
        const labels = trades.map(t => {
            if (!t.open_time) return '#' + t.ticket;
            const d = new Date(t.open_time * 1000);
            const dd = String(d.getDate()).padStart(2, '0');
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const hh = String(d.getHours()).padStart(2, '0');
            const mi = String(d.getMinutes()).padStart(2, '0');
            const ss = String(d.getSeconds()).padStart(2, '0');
            return `${dd}/${mm} ${hh}:${mi}:${ss}`;
        });

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Planned R:R',
                        data: trades.map(t => t.planned_rr),
                        backgroundColor: 'rgba(59,130,246,0.6)',
                    },
                    {
                        label: 'Realized R:R',
                        data: trades.map(t => t.realized_rr),
                        backgroundColor: trades.map(t =>
                            t.realized_rr >= 0 ? 'rgba(16,185,129,0.7)' : 'rgba(239,68,68,0.7)'
                        ),
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#9ca3af' } },
                    title: {
                        display: true,
                        text: 'Recent 10 Trades — Planned vs Realized R:R',
                        color: '#9ca3af', font: { size: 13 },
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#9ca3af', maxRotation: 35, font: { size: 9 } },
                        grid: { display: false },
                    },
                    y: {
                        title: { display: true, text: 'Risk:Reward', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 4: Session Heatmap
    // ══════════════════════════════════════════════════════
    renderSessionHeatmap(canvasId, byDayHour) {
        this._destroy(canvasId);
        const canvas = document.getElementById(canvasId);
        if (!canvas || !byDayHour) return;

        const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
        const hours = Array.from({ length: 24 }, (_, i) => i);

        // Build matrix
        const matrix = [];
        let minPnl = 0, maxPnl = 0;
        for (let di = 0; di < days.length; di++) {
            for (let h = 0; h < 24; h++) {
                const key = `${days[di]}_${String(h).padStart(2, '0')}`;
                const d = byDayHour[key];
                const pnl = d ? d.avg_pnl : null;
                if (pnl !== null) {
                    minPnl = Math.min(minPnl, pnl);
                    maxPnl = Math.max(maxPnl, pnl);
                }
                matrix.push({ x: h, y: di, pnl, count: d ? d.count : 0, winRate: d ? d.win_rate : 0 });
            }
        }

        // Draw on canvas directly (Chart.js doesn't have native heatmap)
        const ctx = canvas.getContext('2d');
        const w = canvas.parentElement.clientWidth - 60;
        const h = 200;
        canvas.width = w;
        canvas.height = h;

        const cellW = (w - 40) / 24;
        const cellH = (h - 30) / days.length;
        const offsetX = 40;
        const offsetY = 5;

        // Clear
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, w, h);

        // Draw cells
        const absMax = Math.max(Math.abs(minPnl), Math.abs(maxPnl)) || 1;
        for (const cell of matrix) {
            const x = offsetX + cell.x * cellW;
            const y = offsetY + cell.y * cellH;

            if (cell.pnl === null) {
                ctx.fillStyle = 'rgba(107,114,128,0.05)';
            } else {
                const intensity = Math.min(Math.abs(cell.pnl) / absMax, 1);
                if (cell.pnl >= 0) {
                    ctx.fillStyle = `rgba(16,185,129,${0.15 + intensity * 0.7})`;
                } else {
                    ctx.fillStyle = `rgba(239,68,68,${0.15 + intensity * 0.7})`;
                }
            }
            ctx.fillRect(x + 1, y + 1, cellW - 2, cellH - 2);

            // Show count if > 0
            if (cell.count > 0) {
                ctx.fillStyle = '#e5e7eb';
                ctx.font = '9px monospace';
                ctx.textAlign = 'center';
                ctx.fillText(cell.count.toString(), x + cellW / 2, y + cellH / 2 + 3);
            }
        }

        // Y-axis labels (days)
        ctx.fillStyle = '#9ca3af';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'right';
        for (let di = 0; di < days.length; di++) {
            ctx.fillText(days[di], offsetX - 4, offsetY + di * cellH + cellH / 2 + 3);
        }

        // X-axis labels (hours)
        ctx.textAlign = 'center';
        for (let hi = 0; hi < 24; hi += 2) {
            ctx.fillText(hi.toString(), offsetX + hi * cellW + cellW / 2, h - 2);
        }

        // Title
        ctx.fillStyle = '#9ca3af';
        ctx.font = '13px sans-serif';
        ctx.textAlign = 'left';

        // Store ref so we can "destroy" by clearing
        this._charts[canvasId] = { destroy: () => { ctx.clearRect(0, 0, w, h); } };
    },

    // ══════════════════════════════════════════════════════
    // Feature 6: Streak Timeline
    // ══════════════════════════════════════════════════════
    renderStreaks(canvasId, data) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !data || !data.trades || !data.trades.length) return;

        // Show only the most recent 30 trades
        const trades = data.trades.slice(-30);
        const labels = trades.map(t => {
            if (!t.close_time) return '#' + t.ticket;
            const d = new Date(t.close_time * 1000);
            return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        });
        const pnls = trades.map(t => t.pnl);
        const colors = pnls.map(p => p >= 0 ? 'rgba(16,185,129,0.8)' : 'rgba(239,68,68,0.8)');

        // Mark revenge trades
        const revengeTickets = new Set(data.revenge_trades.map(r => r.ticket));
        const borderColors = trades.map(t =>
            revengeTickets.has(t.ticket) ? 'rgba(251,191,36,1)' : 'transparent'
        );
        const borderWidths = trades.map(t => revengeTickets.has(t.ticket) ? 2 : 0);

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'PnL',
                    data: pnls,
                    backgroundColor: colors,
                    borderColor: borderColors,
                    borderWidth: borderWidths,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: {
                        display: true,
                        text: `Streaks (last ${trades.length}): Max Win ${data.max_win_streak} | Max Loss ${data.max_loss_streak} | Revenge: ${data.revenge_trades.length}`,
                        color: '#9ca3af', font: { size: 13 },
                    },
                    tooltip: {
                        callbacks: {
                            title(items) {
                                const t = trades[items[0].dataIndex];
                                return '#' + t.ticket;
                            },
                            afterLabel(ctx) {
                                const t = trades[ctx.dataIndex];
                                let tip = `Streak: ${t.streak}`;
                                if (revengeTickets.has(t.ticket)) tip += ' \u26a1 REVENGE TRADE';
                                return tip;
                            }
                        }
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#9ca3af', maxRotation: 45, font: { size: 8 } },
                        grid: { display: false },
                    },
                    y: {
                        title: { display: true, text: 'PnL ($)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 9: Performance by Size
    // ══════════════════════════════════════════════════════
    renderSizePerformance(canvasId, data) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !data || !data.buckets || !data.buckets.length) return;

        const labels = data.buckets.map(b => b.range);

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Win Rate %',
                        data: data.buckets.map(b => +(b.win_rate * 100).toFixed(1)),
                        backgroundColor: 'rgba(16,185,129,0.6)',
                        yAxisID: 'pct',
                    },
                    {
                        label: 'Exit Eff %',
                        data: data.buckets.map(b => +(b.avg_exit_efficiency * 100).toFixed(1)),
                        backgroundColor: 'rgba(59,130,246,0.6)',
                        yAxisID: 'pct',
                    },
                    {
                        label: 'Avg PnL ($)',
                        data: data.buckets.map(b => b.avg_pnl),
                        backgroundColor: data.buckets.map(b =>
                            b.avg_pnl >= 0 ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'
                        ),
                        borderColor: data.buckets.map(b =>
                            b.avg_pnl >= 0 ? 'rgba(16,185,129,0.8)' : 'rgba(239,68,68,0.8)'
                        ),
                        borderWidth: 1,
                        yAxisID: 'dollar',
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#9ca3af' } },
                    title: { display: true, text: 'Performance by Position Size', color: '#9ca3af', font: { size: 13 } },
                    tooltip: {
                        callbacks: {
                            afterBody(items) {
                                const idx = items[0]?.dataIndex;
                                if (idx === undefined) return '';
                                const b = data.buckets[idx];
                                return `Trades: ${b.count} | MAE: $${b.avg_mae.toFixed(2)} | Duration: ${Math.round(b.avg_duration / 60)}min`;
                            }
                        }
                    },
                },
                scales: {
                    x: { ticks: { color: '#9ca3af' }, grid: { display: false } },
                    pct: {
                        position: 'left',
                        title: { display: true, text: '%', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                        min: 0,
                        max: 100,
                    },
                    dollar: {
                        position: 'right',
                        title: { display: true, text: 'Avg PnL ($)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { display: false },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 8: Copy Quality — Slippage Histogram
    // ══════════════════════════════════════════════════════
    renderSlippageHistogram(canvasId, records) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !records || !records.length) return;

        const slippages = records.map(r => r.slippage_points);
        const max = Math.max(...slippages);
        const bucketCount = Math.min(15, Math.max(5, slippages.length));
        const bucketSize = (max / bucketCount) || 0.1;
        const buckets = new Array(bucketCount).fill(0);
        const labels = [];

        for (let i = 0; i < bucketCount; i++) {
            labels.push((i * bucketSize).toFixed(2));
        }

        slippages.forEach(s => {
            let idx = Math.floor(s / bucketSize);
            if (idx >= bucketCount) idx = bucketCount - 1;
            if (idx < 0) idx = 0;
            buckets[idx]++;
        });

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Trades',
                    data: buckets,
                    backgroundColor: 'rgba(251,191,36,0.7)',
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'Slippage Distribution (points)', color: '#9ca3af', font: { size: 13 } },
                },
                scales: {
                    x: {
                        title: { display: true, text: 'Slippage (pts)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { display: false },
                    },
                    y: {
                        title: { display: true, text: 'Count', color: '#6b7280' },
                        ticks: { color: '#9ca3af', stepSize: 1 },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 8: Copy Quality — Slippage vs PnL Scatter
    // ══════════════════════════════════════════════════════
    renderSlippageVsPnl(canvasId, records, closedTrades) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !records || !records.length) return;

        // Build ticket→pnl lookup from closed trades
        const pnlMap = {};
        (closedTrades || []).forEach(t => { pnlMap[t.ticket] = t.pnl; });

        const points = records
            .map(r => ({
                x: r.slippage_points,
                y: pnlMap[r.master_ticket] ?? null,
                ticket: r.master_ticket,
            }))
            .filter(p => p.y !== null);

        if (!points.length) return;

        this._charts[canvasId] = new Chart(ctx, {
            type: 'scatter',
            data: {
                datasets: [{
                    label: 'Trades',
                    data: points,
                    backgroundColor: points.map(p => p.y >= 0 ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)'),
                    pointRadius: 5,
                    pointHoverRadius: 7,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'Slippage vs Trade PnL', color: '#9ca3af', font: { size: 13 } },
                    tooltip: {
                        callbacks: {
                            label(ctx) {
                                const p = ctx.raw;
                                return `#${p.ticket}: ${p.x.toFixed(2)} pts, $${p.y.toFixed(2)}`;
                            }
                        }
                    },
                },
                scales: {
                    x: {
                        title: { display: true, text: 'Slippage (pts)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                    y: {
                        title: { display: true, text: 'PnL ($)', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },

    // ══════════════════════════════════════════════════════
    // Feature 8: Latency Histogram
    // ══════════════════════════════════════════════════════
    renderLatencyHistogram(canvasId, records) {
        this._destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx || !records || !records.length) return;

        const latencies = records.map(r => r.total_latency_ms || r.latency_ms || 0);
        const max = Math.max(...latencies);
        const bucketCount = Math.min(15, Math.max(5, latencies.length));
        const bucketSize = (max / bucketCount) || 10;
        const buckets = new Array(bucketCount).fill(0);
        const labels = [];

        for (let i = 0; i < bucketCount; i++) {
            labels.push(Math.round(i * bucketSize) + 'ms');
        }

        latencies.forEach(l => {
            let idx = Math.floor(l / bucketSize);
            if (idx >= bucketCount) idx = bucketCount - 1;
            if (idx < 0) idx = 0;
            buckets[idx]++;
        });

        this._charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Trades',
                    data: buckets,
                    backgroundColor: 'rgba(139,92,246,0.7)',
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'Copy Latency Distribution', color: '#9ca3af', font: { size: 13 } },
                },
                scales: {
                    x: {
                        title: { display: true, text: 'Latency', color: '#6b7280' },
                        ticks: { color: '#9ca3af' },
                        grid: { display: false },
                    },
                    y: {
                        title: { display: true, text: 'Count', color: '#6b7280' },
                        ticks: { color: '#9ca3af', stepSize: 1 },
                        grid: { color: 'rgba(107,114,128,0.1)' },
                    },
                },
            },
        });
    },
};
