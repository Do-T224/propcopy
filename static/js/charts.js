let equityChart = null;
let pnlChart = null;

function initCharts(closedTrades) {
    if (!closedTrades || !closedTrades.length) return;

    // Equity curve (cumulative PnL)
    const equityCtx = document.getElementById('equityChart');
    if (!equityCtx) return;

    let cumPnl = 0;
    const equityData = closedTrades.map((t, i) => {
        cumPnl += (t.pnl || 0);
        return cumPnl;
    });
    const labels = closedTrades.map((_, i) => i + 1);

    if (equityChart) equityChart.destroy();
    equityChart = new Chart(equityCtx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Cumulative PnL',
                data: equityData,
                borderColor: '#10b981',
                backgroundColor: 'rgba(16, 185, 129, 0.1)',
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: {
                x: {
                    display: true,
                    title: { display: true, text: 'Trade #', color: '#6b7280' },
                    ticks: { color: '#6b7280' },
                    grid: { color: 'rgba(107, 114, 128, 0.1)' },
                },
                y: {
                    title: { display: true, text: 'PnL ($)', color: '#6b7280' },
                    ticks: { color: '#6b7280' },
                    grid: { color: 'rgba(107, 114, 128, 0.1)' },
                },
            },
        },
    });

    // PnL distribution histogram
    const pnlCtx = document.getElementById('pnlChart');
    if (!pnlCtx) return;

    const pnls = closedTrades.map(t => t.pnl || 0);
    const min = Math.min(...pnls);
    const max = Math.max(...pnls);
    const bucketCount = 20;
    const bucketSize = (max - min) / bucketCount || 1;
    const buckets = new Array(bucketCount).fill(0);
    const bucketLabels = [];

    for (let i = 0; i < bucketCount; i++) {
        const low = min + i * bucketSize;
        bucketLabels.push(low.toFixed(0));
    }

    pnls.forEach(p => {
        let idx = Math.floor((p - min) / bucketSize);
        if (idx >= bucketCount) idx = bucketCount - 1;
        if (idx < 0) idx = 0;
        buckets[idx]++;
    });

    const barColors = bucketLabels.map(l => parseFloat(l) >= 0 ? 'rgba(16, 185, 129, 0.7)' : 'rgba(239, 68, 68, 0.7)');

    if (pnlChart) pnlChart.destroy();
    pnlChart = new Chart(pnlCtx, {
        type: 'bar',
        data: {
            labels: bucketLabels,
            datasets: [{
                label: 'Trades',
                data: buckets,
                backgroundColor: barColors,
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: {
                x: {
                    title: { display: true, text: 'PnL ($)', color: '#6b7280' },
                    ticks: { color: '#6b7280', maxTicksLimit: 10 },
                    grid: { display: false },
                },
                y: {
                    title: { display: true, text: 'Count', color: '#6b7280' },
                    ticks: { color: '#6b7280', stepSize: 1 },
                    grid: { color: 'rgba(107, 114, 128, 0.1)' },
                },
            },
        },
    });
}

// Re-init charts when analytics tab is shown
document.addEventListener('alpine:init', () => {
    Alpine.effect(() => {
        const app = Alpine.store('_x_dataStack');
    });
});

// Poll for chart updates only when Overview sub-tab is active
setInterval(() => {
    const tab = document.querySelector('[x-show="activeTab === \'Analytics\'"]');
    if (tab && tab.style.display !== 'none') {
        // Only refresh Overview charts when that sub-tab is shown
        const overviewTab = document.querySelector('[x-show="analyticsSubTab === \'Overview\'"]');
        if (overviewTab && overviewTab.style.display !== 'none') {
            fetch('/api/analytics')
                .then(r => r.json())
                .then(data => {
                    if (data.closed_trades && data.closed_trades.length) {
                        initCharts(data.closed_trades);
                    }
                })
                .catch(() => {});
        }
    }
}, 5000);
