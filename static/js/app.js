function app() {
    return {
        activeTab: 'Status',
        analyticsSubTab: 'Overview',
        state: {
            engine_running: false,
            master: {},
            slaves: [],
            drawdown: {},
            positions: [],
            recent_trades: [],
        },
        logs: [],
        logFilter: 'ALL',
        logSearch: '',
        closedTrades: [],
        insights: null,
        rrPotentialIntervals: [],
        setups: [],
        setupFilter: null,
        historyFilters: { dateFrom: '', dateTo: '', symbol: '' },
        expandedPositions: [],
        showStopModal: false,
        showDeleteModal: false,
        selectedTrades: [],
        startTime: null,
        uptime: '0:00:00',
        toast: { show: false, message: '', type: 'success' },
        ws: null,

        init() {
            this.connectWebSocket();
            this.fetchAnalytics();
            this.fetchSetups();
            setInterval(() => this.updateUptime(), 1000);

            // Watch for analytics sub-tab changes to render charts
            this.$watch('analyticsSubTab', () => this.renderInsightsSubTab());
            this.$watch('activeTab', (tab) => {
                if (tab === 'Analytics') this.fetchInsights();
            });
            this.$watch('setupFilter', () => {
                if (this.activeTab === 'Analytics') this.fetchInsights();
            });
            window.addEventListener('config-saved', () => this.fetchSetups());
            this.$watch('rrPotentialIntervals', () => {
                if (this.analyticsSubTab === 'Risk' && this.insights) {
                    try { InsightCharts.renderRiskRewardLine('riskRewardLineChart', this.insights.risk_reward, this.rrPotentialIntervals); } catch(e) {}
                }
            });
        },

        connectWebSocket() {
            const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            this.ws = new WebSocket(`${proto}//${window.location.host}/ws`);

            this.ws.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                if (msg.type === 'status_update') {
                    const wasRunning = this.state.engine_running;
                    Object.assign(this.state, msg.data);

                    if (msg.data.logs) {
                        this.logs = msg.data.logs;
                        this.$nextTick(() => {
                            const el = document.getElementById('logContainer');
                            if (el) el.scrollTop = el.scrollHeight;
                        });
                    }

                    // Track uptime
                    if (this.state.engine_running && !wasRunning) {
                        this.startTime = Date.now();
                    } else if (!this.state.engine_running) {
                        this.startTime = null;
                    }
                }
            };

            this.ws.onclose = () => {
                setTimeout(() => this.connectWebSocket(), 2000);
            };
        },

        async fetchAnalytics() {
            try {
                const res = await fetch('/api/analytics');
                const data = await res.json();
                this.closedTrades = data.closed_trades || [];
            } catch (e) {}
        },

        async startEngine() {
            try {
                const res = await fetch('/api/engine/start', { method: 'POST' });
                const data = await res.json();
                if (data.ok) {
                    this.showToast('Engine started', 'success');
                    this.startTime = Date.now();
                } else {
                    this.showToast(data.error || 'Failed to start', 'error');
                }
            } catch (e) {
                this.showToast('Connection error', 'error');
            }
        },

        confirmStop() {
            this.showStopModal = true;
        },

        async stopEngine() {
            this.showStopModal = false;
            try {
                const res = await fetch('/api/engine/stop', { method: 'POST' });
                const data = await res.json();
                if (data.ok) {
                    this.showToast('Engine stopped', 'success');
                    this.fetchAnalytics();
                } else {
                    this.showToast(data.error || 'Failed to stop', 'error');
                }
            } catch (e) {
                this.showToast('Connection error', 'error');
            }
        },

        get filteredLogs() {
            let logs = this.logs;
            if (this.logFilter !== 'ALL') {
                logs = logs.filter(l => l.level === this.logFilter);
            }
            if (this.logSearch) {
                const q = this.logSearch.toLowerCase();
                logs = logs.filter(l => l.message.toLowerCase().includes(q));
            }
            return logs;
        },

        updateUptime() {
            if (!this.startTime || !this.state.engine_running) {
                this.uptime = '0:00:00';
                return;
            }
            const secs = Math.floor((Date.now() - this.startTime) / 1000);
            const h = Math.floor(secs / 3600);
            const m = Math.floor((secs % 3600) / 60);
            const s = secs % 60;
            this.uptime = `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
        },

        async fetchSetups() {
            try {
                const res = await fetch('/api/setups');
                const data = await res.json();
                this.setups = data.setups || [];
            } catch (e) {}
        },

        async tagSetup(ticket, setup) {
            try {
                await fetch(`/api/trade/${ticket}/setup`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ setup: setup || null }),
                });
            } catch (e) {}
        },

        async fetchInsights() {
            try {
                let url = '/api/analytics/insights';
                if (this.setupFilter) url += '?setup=' + encodeURIComponent(this.setupFilter);
                const res = await fetch(url);
                this.insights = await res.json();
                this.$nextTick(() => this.renderInsightsSubTab());
            } catch (e) {}
        },

        renderInsightsSubTab() {
            if (!this.insights) return;
            // Use setTimeout to ensure x-show has toggled display and layout is complete
            setTimeout(() => {
                const tab = this.analyticsSubTab;
                InsightCharts.destroyAll();
                if (tab === 'Overview') {
                    if (this.closedTrades.length) initCharts(this.closedTrades);
                } else if (tab === 'Entry & Exit') {
                    InsightCharts.renderEntryTiming('entryTimingChart', this.insights.entry_timing?.aggregate);
                    InsightCharts.renderExitTiming('exitTimingChart', this.insights.exit_timing?.aggregate);
                    InsightCharts.renderExitEfficiency('exitEfficiencyChart', this.insights.exit_efficiency);
                    InsightCharts.renderDurationOutcome('durationOutcomeChart', this.insights.duration_outcome);
                } else if (tab === 'Risk') {
                    try { InsightCharts.renderExitDistribution('exitDistChart', this.insights.sl_tp_analysis?.exit_distribution); } catch(e) { console.error('exitDist:', e); }
                    try { InsightCharts.renderRiskRewardLine('riskRewardLineChart', this.insights.risk_reward, this.rrPotentialIntervals); } catch(e) { console.error('rrLine:', e); }
                    try { InsightCharts.renderRiskRewardBar('riskRewardBarChart', this.insights.risk_reward); } catch(e) { console.error('rrBar:', e); }
                } else if (tab === 'Patterns') {
                    InsightCharts.renderSessionHeatmap('sessionHeatmap', this.insights.session_performance?.by_day_hour);
                    InsightCharts.renderStreaks('streakChart', this.insights.streaks);
                    InsightCharts.renderSizePerformance('sizePerformanceChart', this.insights.size_performance);
                } else if (tab === 'Copy Quality') {
                    const cq = this.insights.copy_quality;
                    if (cq) {
                        InsightCharts.renderSlippageHistogram('slippageHistChart', cq.records);
                        InsightCharts.renderSlippageVsPnl('slippagePnlChart', cq.records, this.closedTrades);
                        InsightCharts.renderLatencyHistogram('latencyHistChart', cq.records);
                    }
                }
            }, 50);
        },

        get filteredClosedTrades() {
            let trades = this.closedTrades;
            const f = this.historyFilters;
            if (f.dateFrom) {
                const from = new Date(f.dateFrom).getTime() / 1000;
                trades = trades.filter(t => t.close_time >= from);
            }
            if (f.dateTo) {
                const to = new Date(f.dateTo + 'T23:59:59').getTime() / 1000;
                trades = trades.filter(t => t.close_time <= to);
            }
            if (f.symbol) {
                trades = trades.filter(t => t.symbol === f.symbol);
            }
            return trades;
        },

        get historySummary() {
            const trades = this.filteredClosedTrades;
            if (!trades.length) return { total_pnl: 0, count: 0, win_rate: 0, avg_pnl: 0 };
            const total_pnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
            const wins = trades.filter(t => t.pnl > 0).length;
            return {
                total_pnl,
                count: trades.length,
                win_rate: trades.length ? (wins / trades.length * 100) : 0,
                avg_pnl: trades.length ? total_pnl / trades.length : 0,
            };
        },

        get historySymbols() {
            return [...new Set(this.closedTrades.map(t => t.symbol).filter(Boolean))].sort();
        },

        get sessionStats() {
            const todayStart = new Date();
            todayStart.setUTCHours(0, 0, 0, 0);
            const ts = Math.floor(todayStart.getTime() / 1000);
            const todayTrades = this.closedTrades.filter(t => t.close_time >= ts);
            const pnl = todayTrades.reduce((s, t) => s + (t.pnl || 0), 0);
            const wins = todayTrades.filter(t => t.pnl > 0).length;
            const losses = todayTrades.filter(t => t.pnl <= 0).length;
            const winRate = todayTrades.length ? (wins / todayTrades.length * 100) : 0;
            // Avg latency from today's copy_latency
            const latencies = (this.state.copy_latency || []).filter(l => l.timestamp >= ts);
            const avgLatency = latencies.length ? latencies.reduce((s, l) => s + (l.total_latency_ms || l.latency_ms || 0), 0) / latencies.length : 0;
            return { pnl, wins, losses, winRate, avgLatency, count: todayTrades.length };
        },

        togglePosition(ticket) {
            const idx = this.expandedPositions.indexOf(ticket);
            if (idx >= 0) this.expandedPositions.splice(idx, 1);
            else this.expandedPositions.push(ticket);
        },

        getSlaveLatency(slaveAccount) {
            const entries = (this.state.copy_latency || []).filter(l => l.slave_account == slaveAccount);
            if (!entries.length) return null;
            const last = entries[entries.length - 1];
            return {
                total_latency_ms: last.total_latency_ms || last.latency_ms || 0,
                true_latency_ms: last.true_latency_ms || 0,
                server_latency_ms: last.server_latency_ms || last.latency_ms || 0,
                timestamp: last.timestamp,
            };
        },

        formatDuration(seconds) {
            if (!seconds) return '-';
            if (seconds < 60) return seconds + 's';
            if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            return h + 'h ' + m + 'm';
        },

        async toggleAccount(accountId) {
            try {
                const res = await fetch(`/api/account/${accountId}/toggle`, { method: 'POST' });
                const data = await res.json();
                if (data.ok) {
                    this.showToast(`Account ${accountId} ${data.suspended ? 'paused' : 'resumed'}`, 'success');
                } else {
                    this.showToast(data.error || 'Toggle failed', 'error');
                }
            } catch (e) {
                this.showToast('Connection error', 'error');
            }
        },

        async toggleProfitTarget(accountId, enabled) {
            try {
                const res = await fetch(`/api/profit-target/toggle/${accountId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled }),
                });
                const data = await res.json();
                if (!data.ok) {
                    this.showToast(data.error || 'Failed to toggle profit target', 'error');
                }
            } catch (e) {
                this.showToast('Connection error', 'error');
            }
        },

        async resetGuardian(accountId) {
            try {
                const res = await fetch(`/api/guardian/reset/${accountId}`, { method: 'POST' });
                const data = await res.json();
                if (data.ok) {
                    this.showToast(`Guardian reset for slave ${accountId}`, 'success');
                } else {
                    this.showToast(data.error || 'Failed to reset guardian', 'error');
                }
            } catch (e) {
                this.showToast('Connection error', 'error');
            }
        },

        toggleTrade(ticket) {
            const idx = this.selectedTrades.indexOf(ticket);
            if (idx >= 0) this.selectedTrades.splice(idx, 1);
            else this.selectedTrades.push(ticket);
        },

        toggleAllTrades(checked) {
            if (checked) {
                this.selectedTrades = this.filteredClosedTrades.map(t => t.ticket);
            } else {
                this.selectedTrades = [];
            }
        },

        exportSelectedTrades() {
            const trades = this.closedTrades.filter(t => this.selectedTrades.includes(t.ticket));
            if (!trades.length) return;
            const headers = ['ticket', 'symbol', 'type', 'volume', 'open_price', 'close_price', 'pnl', 'mfe', 'mae', 'duration_seconds', 'open_time', 'close_time', 'setup'];
            const csv = [headers.join(',')];
            for (const t of trades) {
                csv.push(headers.map(h => {
                    const v = t[h];
                    return v === undefined || v === null ? '' : v;
                }).join(','));
            }
            const blob = new Blob([csv.join('\n')], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `propcopy_trades_${new Date().toISOString().slice(0,10)}.csv`;
            a.click();
            URL.revokeObjectURL(url);
            this.showToast(`Exported ${trades.length} trade(s)`, 'success');
        },

        async deleteTrades() {
            this.showDeleteModal = false;
            if (!this.selectedTrades.length) return;
            try {
                const res = await fetch('/api/analytics/trades/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tickets: this.selectedTrades }),
                });
                const data = await res.json();
                if (data.ok) {
                    this.showToast(`Deleted ${data.removed} trade(s)`, 'success');
                    this.selectedTrades = [];
                    this.fetchAnalytics();
                    if (this.activeTab === 'Analytics') this.fetchInsights();
                } else {
                    this.showToast(data.error || 'Delete failed', 'error');
                }
            } catch (e) {
                this.showToast('Connection error', 'error');
            }
        },

        showToast(message, type = 'success') {
            this.toast = { show: true, message, type };
            setTimeout(() => { this.toast.show = false; }, 3000);
        },
    };
}

function configForm() {
    return {
        config: {
            master: { account: 0, password: '', server: '', mt5_path: '' },
            slaves: [],
            settings: {},
            data: {},
            _master_tag: '',
            _slave_tags: [],
        },
        accounts: [],
        masterTag: '',
        slaveTags: [],  // [{account_tag, size_scaler}]
        futuresFollowers: [],  // [{account, platform, account_name, symbol_map, size_scaler, ...}] (ninjatrader)
        showFutures: true,
        editingAccount: null,  // null or {tag, account, password, server, mt5_path, _isNew}
        showEditPw: false,
        saveStatus: '',
        accountsStatus: '',
        showAccounts: true,
        showMaster: true,
        showSlaves: true,
        showSettings: false,
        showSetups: false,

        async loadConfig() {
            try {
                const [configRes, accountsRes] = await Promise.all([
                    fetch('/api/config'),
                    fetch('/api/accounts'),
                ]);
                this.config = await configRes.json();
                this.accounts = await accountsRes.json();
                // Futures followers (ninjatrader) are not in the accounts library; a few
                // knobs are editable here, identity stays in config.yaml.
                this.futuresFollowers = (this.config.slaves || [])
                    .filter(s => s.platform === 'ninjatrader')
                    .map(s => ({
                        account: s.account,
                        platform: s.platform,
                        account_name: s.account_name || '',
                        symbol_map: s.symbol_map || {},
                        size_scaler: s.size_scaler ?? 1.0,
                        sltp_multiplier: s.sltp_multiplier ?? 1.0,
                        max_drawdown_pct: s.max_drawdown_pct ?? 0,
                        profit_target_usd: s.profit_target_usd ?? 0,
                    }));
                // Unpack stagger_ms array into separate fields for UI
                const sm = this.config.settings.stagger_ms || [0, 0];
                this.config.settings.stagger_ms_min = sm[0] || 0;
                this.config.settings.stagger_ms_max = sm[1] || 0;
                // Build slave tags data (but don't assign yet — let accounts render first)
                let newSlaveTags = (this.config._slave_tags || []).map(s => ({
                    account_tag: s.account_tag || '',
                    size_scaler: s.size_scaler || 1.0,
                    max_drawdown_pct: s.max_drawdown_pct || 0,
                    profit_target_usd: s.profit_target_usd || 0,
                    sltp_multiplier: s.sltp_multiplier || 1.0,
                    invert: s.invert || false,
                    price_offset_points: s.price_offset_points || 0,
                    price_offset_timeout: s.price_offset_timeout || 20,
                    price_offset_max_points: s.price_offset_max_points || 0,
                    volume_jitter_pct: s.volume_jitter_pct || 0,
                    sltp_offset_points: s.sltp_offset_points || 0,
                    _showStagger: false,
                }));
                let newMasterTag = this.config._master_tag || '';
                // If no tags saved yet, try to match existing config to accounts by account number
                if (!newMasterTag && this.config.master.account) {
                    const match = this.accounts.find(a => a.account === this.config.master.account);
                    if (match) newMasterTag = match.tag;
                }
                const mtSlaves = this.config.slaves.filter(s => s.platform !== 'ninjatrader');
                if (!newSlaveTags.length && mtSlaves.length) {
                    newSlaveTags = mtSlaves.map(s => {
                        const match = this.accounts.find(a => a.account === s.account);
                        return {
                            account_tag: match ? match.tag : '',
                            size_scaler: s.size_scaler || 1.0,
                            max_drawdown_pct: s.max_drawdown_pct || 0,
                            profit_target_usd: s.profit_target_usd || 0,
                            sltp_multiplier: s.sltp_multiplier || 1.0,
                            invert: s.invert || false,
                            price_offset_points: s.price_offset_points || 0,
                            price_offset_timeout: s.price_offset_timeout || 20,
                            price_offset_max_points: s.price_offset_max_points || 0,
                            volume_jitter_pct: s.volume_jitter_pct || 0,
                            sltp_offset_points: s.sltp_offset_points || 0,
                            _showStagger: false,
                        };
                    });
                }
                // Set slaveTags with blank selections first so Alpine renders rows + <option> elements,
                // then set actual values after a tick so x-model can match them.
                this.slaveTags = newSlaveTags.map(s => ({ ...s, account_tag: '' }));
                this.masterTag = '';
                await new Promise(r => setTimeout(r, 0));
                this.slaveTags = newSlaveTags;
                this.masterTag = newMasterTag;
            } catch (e) {}
        },

        addSlave() {
            this.slaveTags.push({
                account_tag: '', size_scaler: 1.0, max_drawdown_pct: 0,
                profit_target_usd: 0, sltp_multiplier: 1.0, invert: false,
                price_offset_points: 0, price_offset_timeout: 20, price_offset_max_points: 0,
                volume_jitter_pct: 0, sltp_offset_points: 0,
                _showStagger: false,
            });
        },

        removeSlave(idx) {
            this.slaveTags.splice(idx, 1);
        },

        // ── Accounts Library ──

        openNewAccount() {
            this.editingAccount = { tag: '', account: 0, password: '', server: '', mt5_path: '', platform: 'mt5', mt4_port: 15555, _isNew: true };
            this.showEditPw = false;
        },

        openEditAccount(acct) {
            this.editingAccount = { ...acct, platform: acct.platform || 'mt5', mt4_port: acct.mt4_port || 15555, _isNew: false, _origTag: acct.tag };
            this.showEditPw = false;
        },

        cancelEdit() {
            this.editingAccount = null;
        },

        async saveAccount() {
            const ea = this.editingAccount;
            if (!ea.tag.trim()) {
                this.accountsStatus = 'Tag is required';
                setTimeout(() => { this.accountsStatus = ''; }, 3000);
                return;
            }
            const updated = [...this.accounts];
            if (ea._isNew) {
                if (updated.some(a => a.tag === ea.tag)) {
                    this.accountsStatus = 'Tag already exists';
                    setTimeout(() => { this.accountsStatus = ''; }, 3000);
                    return;
                }
                const acctData = { tag: ea.tag, account: ea.account, password: ea.password, server: ea.server, platform: ea.platform || 'mt5' };
                if (ea.platform === 'mt4') { acctData.mt4_port = ea.mt4_port || 15555; } else { acctData.mt5_path = ea.mt5_path; }
                updated.push(acctData);
            } else {
                const idx = updated.findIndex(a => a.tag === ea._origTag);
                if (idx >= 0) {
                    // Update tag references in slaveTags and masterTag
                    if (ea._origTag !== ea.tag) {
                        if (this.masterTag === ea._origTag) this.masterTag = ea.tag;
                        this.slaveTags.forEach(s => { if (s.account_tag === ea._origTag) s.account_tag = ea.tag; });
                    }
                    const acctData = { tag: ea.tag, account: ea.account, password: ea.password, server: ea.server, platform: ea.platform || 'mt5' };
                    if (ea.platform === 'mt4') { acctData.mt4_port = ea.mt4_port || 15555; } else { acctData.mt5_path = ea.mt5_path; }
                    updated[idx] = acctData;
                }
            }
            try {
                const res = await fetch('/api/accounts', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(updated),
                });
                const data = await res.json();
                if (data.ok) {
                    // Reload accounts to get masked passwords back
                    const accountsRes = await fetch('/api/accounts');
                    this.accounts = await accountsRes.json();
                    this.editingAccount = null;
                } else {
                    this.accountsStatus = data.error || 'Error';
                    setTimeout(() => { this.accountsStatus = ''; }, 3000);
                }
            } catch (e) {
                this.accountsStatus = 'Connection error';
                setTimeout(() => { this.accountsStatus = ''; }, 3000);
            }
        },

        async deleteAccount(tag) {
            if (this.masterTag === tag) {
                this.accountsStatus = 'Cannot delete: used as master';
                setTimeout(() => { this.accountsStatus = ''; }, 3000);
                return;
            }
            if (this.slaveTags.some(s => s.account_tag === tag)) {
                this.accountsStatus = 'Cannot delete: used as slave';
                setTimeout(() => { this.accountsStatus = ''; }, 3000);
                return;
            }
            const updated = this.accounts.filter(a => a.tag !== tag);
            try {
                const res = await fetch('/api/accounts', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(updated),
                });
                const data = await res.json();
                if (data.ok) this.accounts = updated;
            } catch (e) {}
        },

        accountLabel(tag) {
            const acct = this.accounts.find(a => a.tag === tag);
            if (!acct) return tag;
            const platform = (acct.platform || 'mt5').toUpperCase();
            return `[${platform}] ${acct.tag} (${acct.account} @ ${acct.server})`;
        },

        // ── Save Config ──

        async saveConfig() {
            // Pack stagger_ms back into array for backend
            const settings = { ...this.config.settings };
            settings.stagger_ms = [settings.stagger_ms_min || 0, settings.stagger_ms_max || 0];
            delete settings.stagger_ms_min;
            delete settings.stagger_ms_max;
            const payload = {
                master_tag: this.masterTag,
                slaves: this.slaveTags.filter(s => s.account_tag),
                futures_followers: this.futuresFollowers.map(f => ({
                    account: f.account,
                    size_scaler: f.size_scaler,
                    sltp_multiplier: f.sltp_multiplier,
                    max_drawdown_pct: f.max_drawdown_pct,
                    profit_target_usd: f.profit_target_usd,
                })),
                settings: settings,
                data: this.config.data,
            };
            try {
                const res = await fetch('/api/config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                this.saveStatus = data.ok ? 'Saved!' : (data.error || 'Error');
                if (data.ok) {
                    window.dispatchEvent(new CustomEvent('config-saved'));
                    // Reload config to sync UI with saved state
                    await this.loadConfig();
                }
                setTimeout(() => { this.saveStatus = ''; }, 3000);
            } catch (e) {
                this.saveStatus = 'Connection error';
                setTimeout(() => { this.saveStatus = ''; }, 3000);
            }
        },
    };
}
