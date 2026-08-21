document.addEventListener('DOMContentLoaded', () => {
    // Every chart div gets re-plotted repeatedly during a session (new day
    // clicked, new backtest run, etc.). plotlyRedraw() on the same div
    // without purging first can leave the previous plot's internal state
    // (traces, event listeners) hanging around, leaking memory over a long
    // session. Purging first is a no-op / harmless if the div was never
    // plotted, so this is always safe to call.
    function plotlyRedraw(divId, data, layout, config) {
        try {
            const el = (typeof divId === 'string') ? document.getElementById(divId) : divId;
            if (el) Plotly.purge(el);
        } catch (e) { /* nothing to purge yet, ignore */ }
        return Plotly.newPlot(divId, data, layout, config);
    }

    // Basic elements
    const strategyDropdown = document.getElementById('strategyDropdown');
    const btnRefreshStrategies = document.getElementById('btnRefreshStrategies');
    const scriptPathInput = document.getElementById('scriptPath');

    const dataDropdown = document.getElementById('dataDropdown');
    const btnRefreshData = document.getElementById('btnRefreshData');
    const dataPathInput = document.getElementById('dataPath');
    const paramsContainer = document.getElementById('paramsContainer');
    const btnRun = document.getElementById('btnRun');
    const statusIndicator = document.getElementById('statusIndicator');
    const metricsGrid = document.getElementById('metricsGrid');
    const loadingOverlay = document.getElementById('loadingOverlay');
    const mainContent = document.getElementById('mainContent');

    // Preset elements
    const btnSavePreset = document.getElementById('btnSavePreset');
    const presetDropdown = document.getElementById('presetDropdown');
    const btnLoadPreset = document.getElementById('btnLoadPreset');
    const btnDeletePreset = document.getElementById('btnDeletePreset');

    // Testing Period Quarter Slider elements
    const quarterStartRange = document.getElementById('quarterStartRange');
    const quarterEndRange = document.getElementById('quarterEndRange');
    const quarterSliderHighlight = document.getElementById('quarterSliderHighlight');
    const startQuarterLabel = document.getElementById('startQuarterLabel');
    const endQuarterLabel = document.getElementById('endQuarterLabel');
    const sliderMinLabel = document.getElementById('sliderMinLabel');
    const sliderMaxLabel = document.getElementById('sliderMaxLabel');
    const quarterCountBadge = document.getElementById('quarterCountBadge');

    let availableQuarters = [];
    let selectedStartQuarter = '';
    let selectedEndQuarter = '';

    // Config panel buttons
    const btnRunMC = document.getElementById('btnRunMC');
    const btnRunDist = document.getElementById('btnRunDist');
    const btnRunRobustness = document.getElementById('btnRunRobustness');

    const robustnessTable = document.querySelector('#robustnessTable tbody');

    let currentScript = '';
    let currentData = '';

    // ---- Symbol pill: derive instrument symbol(s) from selected data file(s) ----
    // Filenames look like SYMBOL_whatever_2025.parquet -> symbol is the text
    // before the first underscore, extension stripped first.
    function getSymbolFromPath(path) {
        const filename = path.split('\\').pop().split('/').pop();
        const noExt = filename.replace(/\.[^/.]+$/, '');
        return noExt.split('_')[0].toUpperCase();
    }
    function getSymbolLabel(data) {
        if (!data || (Array.isArray(data) && data.length === 0)) return '—';
        const paths = Array.isArray(data) ? data : [data];
        const symbols = [...new Set(paths.map(getSymbolFromPath))];
        return symbols.join(' // ');
    }
    function updateSymbolPill() {
        const el = document.getElementById('symbolPill');
        if (el) el.textContent = getSymbolLabel(currentData);
    }
    let lastMCResults = null;
    let lastEquityAll = [];
    let lastEquityLong = [];
    let lastEquityShort = [];

    window.currentCalYear = null;
    window.currentCalMonth = null;
    window.currentChartDate = null;
    window.validTradingDays = [];

    window.prevMonth = function () {
        if (!window.currentCalYear || !window.currentCalMonth) return;
        let y = window.currentCalYear;
        let m = window.currentCalMonth - 1;
        if (m < 1) { m = 12; y--; }
        window.openCalendarModal(y, m);
    };

    window.nextMonth = function () {
        if (!window.currentCalYear || !window.currentCalMonth) return;
        let y = window.currentCalYear;
        let m = window.currentCalMonth + 1;
        if (m > 12) { m = 1; y++; }
        window.openCalendarModal(y, m);
    };

    window.autoLoadDay = null;

    window.backToCalendar = function () {
        document.getElementById('dayChartArea').style.display = 'none';
        document.getElementById('calendarTopHeader').style.display = 'flex';
        document.getElementById('calendarViewArea').style.display = 'flex';
    };

    window.prevDay = function () {
        if (!window.validTradingDays || !window.currentChartDate) return;
        let idx = window.validTradingDays.indexOf(window.currentChartDate);
        if (idx > 0) {
            loadDayChart(window.validTradingDays[idx - 1]);
        } else {
            window.autoLoadDay = 'last';
            window.prevMonth();
        }
    };

    window.nextDay = function () {
        if (!window.validTradingDays || !window.currentChartDate) return;
        let idx = window.validTradingDays.indexOf(window.currentChartDate);
        if (idx >= 0 && idx < window.validTradingDays.length - 1) {
            loadDayChart(window.validTradingDays[idx + 1]);
        } else {
            window.autoLoadDay = 'first';
            window.nextMonth();
        }
    };

    let lastHeatmapAll = [];
    let lastHeatmapLong = [];
    let lastHeatmapShort = [];

    // Equity Toggle Buttons
    const btnShowAll = document.getElementById('btnShowAll');
    const btnShowLong = document.getElementById('btnShowLong');
    const btnShowShort = document.getElementById('btnShowShort');

    if (btnShowAll) {
        btnShowAll.addEventListener('click', () => {
            btnShowAll.classList.add('primary');
            btnShowLong.classList.remove('primary');
            btnShowShort.classList.remove('primary');
            plotEquity('plotEquityMain', lastEquityAll, '#1a8c54', 'All Trades');
        });
        btnShowLong.addEventListener('click', () => {
            btnShowLong.classList.add('primary');
            btnShowAll.classList.remove('primary');
            btnShowShort.classList.remove('primary');
            plotEquity('plotEquityMain', lastEquityLong, '#3b82f6', 'Long Trades');
        });
        btnShowShort.addEventListener('click', () => {
            btnShowShort.classList.add('primary');
            btnShowAll.classList.remove('primary');
            btnShowLong.classList.remove('primary');
            plotEquity('plotEquityMain', lastEquityShort, '#ef4444', 'Short Trades');
        });
    }

    // Heatmap Toggle Buttons
    const btnHeatmapAll = document.getElementById('btnHeatmapAll');
    const btnHeatmapLong = document.getElementById('btnHeatmapLong');
    const btnHeatmapShort = document.getElementById('btnHeatmapShort');

    if (btnHeatmapAll) {
        btnHeatmapAll.addEventListener('click', () => {
            btnHeatmapAll.classList.add('primary');
            btnHeatmapLong.classList.remove('primary');
            btnHeatmapShort.classList.remove('primary');
            renderHeatmap(lastHeatmapAll);
        });
        btnHeatmapLong.addEventListener('click', () => {
            btnHeatmapLong.classList.add('primary');
            btnHeatmapAll.classList.remove('primary');
            btnHeatmapShort.classList.remove('primary');
            renderHeatmap(lastHeatmapLong);
        });
        btnHeatmapShort.addEventListener('click', () => {
            btnHeatmapShort.classList.add('primary');
            btnHeatmapAll.classList.remove('primary');
            btnHeatmapLong.classList.remove('primary');
            renderHeatmap(lastHeatmapShort);
        });
    }

    function setStatus(text, color = 'var(--text-secondary)') {
        // Status indicator removed.
    }

    function formatValue(key, val) {
        if (val === undefined || val === null || val === 'N/A') return 'N/A';
        if (key === 'P-Value') return (parseFloat(val) * 100).toFixed(2) + '%';
        if (key.includes('(%)') || key === 'Percentage of Winning Days' || key === 'Top 1%' || key === 'Top 5%' || key === 'Top 10%') return parseFloat(val).toFixed(2) + '%';
        if (key.includes('Duration')) return parseFloat(val).toFixed(1) + ' m';
        if (key.includes('Frequency') || key.includes('Ratio') || key.includes('Factor') || key === 'Sharpe' || key === 'Sortino' || key === 'RoMD' || key === 'Trades' || key.includes('Consec')) {
            return Number.isInteger(val) ? val.toString() : parseFloat(val).toFixed(2);
        }
        if (key === 'Expectancy ($)' || key === 'Commission Paid' || key === 'Slippage Paid' || key === 'Net Profit' || (key.includes('Trade') && key !== 'Trades')) {
            const num = parseFloat(val);
            return (num < 0 ? '-$' : '$') + Math.abs(num).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        if (typeof val === 'number' && !Number.isInteger(val)) return parseFloat(val).toFixed(2);
        return val;
    }

    // --- PRESETS LOGIC ---
    function loadPresets() {
        fetch('/api/presets')
            .then(response => response.json())
            .then(data => {
                presetDropdown.innerHTML = '<option value="">-- Select Saved Layout --</option>';
                if (data && data.presets) {
                    for (const [name, preset] of Object.entries(data.presets)) {
                        const option = document.createElement('option');
                        option.value = JSON.stringify(preset);
                        option.textContent = name;
                        presetDropdown.appendChild(option);
                    }
                }
            })
            .catch(e => console.error("Error loading presets:", e));
    }

    // Load presets on startup
    loadPresets();

    if (btnSavePreset) {
        btnSavePreset.addEventListener('click', () => {
            if (!currentScript || !currentData) {
                alert("You must select both a script and a data source to save a layout.");
                return;
            }
            const name = prompt("Enter name for this layout:");
            if (!name) return;

            fetch('/api/presets', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    script: currentScript,
                    data: currentData,
                    start_quarter: selectedStartQuarter,
                    end_quarter: selectedEndQuarter,
                    params: getParamsFromUI()
                })
            })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        setStatus('Layout saved successfully!', 'var(--success)');
                        loadPresets();
                    } else {
                        alert("Error saving layout: " + (data.error || "Unknown error"));
                    }
                })
                .catch(e => alert("Network error saving layout"));
        });
    }

    if (btnLoadPreset) {
        btnLoadPreset.addEventListener('click', () => {
            if (!presetDropdown || !presetDropdown.value) {
                alert("Please select a layout from the dropdown first.");
                return;
            }
            try {
                const preset = JSON.parse(presetDropdown.value);
                currentScript = preset.script;
                currentData = preset.data;

                selectStrategyByPath(currentScript);
                selectDataByPath(currentData, null, preset.start_quarter, preset.end_quarter);
                if (preset.params) {
                    setTimeout(() => {
                        Object.entries(preset.params).forEach(([k, v]) => {
                            const input = document.querySelector(`[data-key="${k}"]`);
                            if (input) input.value = v;
                        });
                    }, 250);
                }
                setStatus('Layout loaded successfully', 'var(--success)');
            } catch (e) {
                console.error("Error parsing preset", e);
            }
        });
    }

    if (btnDeletePreset) {
        btnDeletePreset.addEventListener('click', () => {
            if (!presetDropdown || !presetDropdown.value) {
                alert("Please select a layout from the dropdown first.");
                return;
            }
            const selectedOption = presetDropdown.options[presetDropdown.selectedIndex];
            const name = selectedOption.textContent;

            if (!confirm(`Are you sure you want to delete the layout "${name}"?`)) {
                return;
            }

            fetch('/api/presets', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name })
            })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        setStatus('Layout deleted', 'var(--success)');
                        loadPresets();
                    } else {
                        alert("Error deleting layout: " + (data.error || "Unknown error"));
                    }
                })
                .catch(e => alert("Network error deleting layout"));
        });
    }

    // --- MULTI-MARKET PRESETS LOGIC ---
    const mmPresetDropdown = document.getElementById('mmPresetDropdown');
    const btnLoadMMPreset = document.getElementById('btnLoadMMPreset');
    const btnSaveMMPreset = document.getElementById('btnSaveMMPreset');
    const btnDeleteMMPreset = document.getElementById('btnDeleteMMPreset');

    function loadMMPresets() {
        if (!mmPresetDropdown) return;
        fetch('/api/mm_presets')
            .then(response => response.json())
            .then(data => {
                mmPresetDropdown.innerHTML = '<option value="">Select Layout...</option>';
                if (data && data.presets) {
                    for (const [name, preset] of Object.entries(data.presets)) {
                        const option = document.createElement('option');
                        option.value = JSON.stringify(preset);
                        option.textContent = name;
                        mmPresetDropdown.appendChild(option);
                    }
                }
            })
            .catch(e => console.error("Error loading MM presets:", e));
    }

    loadMMPresets();

    if (btnSaveMMPreset) {
        btnSaveMMPreset.addEventListener('click', () => {
            if (secondaryDataPaths.length === 0) {
                alert("You must select at least one additional market to save a layout.");
                return;
            }
            const name = prompt("Enter name for this Multi-Market layout:");
            if (!name) return;

            fetch('/api/mm_presets', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    data_paths: secondaryDataPaths
                })
            })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        setStatus('MM Layout saved successfully!', 'var(--success)');
                        loadMMPresets();
                    } else {
                        alert("Error saving layout: " + (data.error || "Unknown error"));
                    }
                })
                .catch(e => alert("Network error saving layout"));
        });
    }

    if (btnLoadMMPreset) {
        btnLoadMMPreset.addEventListener('click', () => {
            if (!mmPresetDropdown.value) {
                alert("Please select a layout from the dropdown first.");
                return;
            }
            const preset = JSON.parse(mmPresetDropdown.value);
            secondaryDataPaths = preset.data_paths || [];

            if (secondaryDataPaths.length > 0) {
                document.getElementById('multiMarketSelectedFiles').innerText = `${secondaryDataPaths.length} additional market(s) selected:\n` + secondaryDataPaths.join('\n');
            } else {
                document.getElementById('multiMarketSelectedFiles').innerText = 'No additional markets selected.';
            }
            setStatus('MM Layout loaded successfully.', 'var(--success)');
        });
    }

    if (btnDeleteMMPreset) {
        btnDeleteMMPreset.addEventListener('click', () => {
            if (!mmPresetDropdown.value) {
                alert("Please select a layout from the dropdown first.");
                return;
            }
            const selectedOption = mmPresetDropdown.options[mmPresetDropdown.selectedIndex];
            const name = selectedOption.textContent;

            if (!confirm(`Are you sure you want to delete the layout "${name}"?`)) {
                return;
            }

            fetch('/api/mm_presets', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name })
            })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        setStatus('MM Layout deleted', 'var(--success)');
                        loadMMPresets();
                    } else {
                        alert("Error deleting layout: " + (data.error || "Unknown error"));
                    }
                })
                .catch(e => alert("Network error deleting layout"));
        });
    }
    // ----------------------

    // ---- Strategy & Market Data Auto-Discovery & Dropdown Management ----
    let availableStrategies = [];
    let availableDatasets = [];

    function setStrategyDropdownValue(path) {
        if (!strategyDropdown) return;
        let found = false;
        for (let i = 0; i < strategyDropdown.options.length; i++) {
            if (strategyDropdown.options[i].value === path) {
                strategyDropdown.selectedIndex = i;
                found = true;
                break;
            }
        }
        if (!found && path) {
            const filename = path.split('\\').pop().split('/').pop();
            const opt = document.createElement('option');
            opt.value = path;
            opt.textContent = filename;
            strategyDropdown.appendChild(opt);
            strategyDropdown.value = path;
        }
    }

    function selectStrategyByPath(path, displayName) {
        if (!path) return;
        currentScript = path;
        const filename = displayName || path.split('\\').pop().split('/').pop();
        if (scriptPathInput) scriptPathInput.value = filename;
        setStrategyDropdownValue(path);
        loadParams(currentScript);
    }

    function loadLocalStrategies(autoSelectFirst = true) {
        if (!strategyDropdown) return Promise.resolve();
        strategyDropdown.innerHTML = '<option value="">Scanning Strategy_Files...</option>';
        return fetch('/api/local_strategies')
            .then(r => r.json())
            .then(data => {
                availableStrategies = data.strategies || [];
                strategyDropdown.innerHTML = '';
                
                if (availableStrategies.length === 0) {
                    const opt = document.createElement('option');
                    opt.value = '';
                    opt.textContent = '(No .py strategies in Strategy_Files)';
                    strategyDropdown.appendChild(opt);
                } else {
                    availableStrategies.forEach(s => {
                        const opt = document.createElement('option');
                        opt.value = s.path;
                        opt.textContent = s.display_name;
                        strategyDropdown.appendChild(opt);
                    });
                }

                if (currentScript) {
                    setStrategyDropdownValue(currentScript);
                } else if (autoSelectFirst && availableStrategies.length > 0) {
                    selectStrategyByPath(availableStrategies[0].path, availableStrategies[0].display_name);
                }
            })
            .catch(e => {
                console.error("Error loading strategies:", e);
                strategyDropdown.innerHTML = '<option value="">Error loading strategies</option>';
            });
    }

    function setDataDropdownValue(data) {
        if (!dataDropdown) return;
        if (Array.isArray(data)) {
            if (data.length === 1) {
                setDataDropdownValue(data[0]);
            } else {
                const label = `${data.length} files selected`;
                let opt = dataDropdown.querySelector('option[value="__multi__"]');
                if (!opt) {
                    opt = document.createElement('option');
                    opt.value = '__multi__';
                    dataDropdown.appendChild(opt);
                }
                opt.textContent = label;
                dataDropdown.value = '__multi__';
            }
            return;
        }
        const path = data;
        let found = false;
        for (let i = 0; i < dataDropdown.options.length; i++) {
            if (dataDropdown.options[i].value === path) {
                dataDropdown.selectedIndex = i;
                found = true;
                break;
            }
        }
        if (!found && path && path !== '__multi__') {
            const filename = path.split('\\').pop().split('/').pop();
            const opt = document.createElement('option');
            opt.value = path;
            opt.textContent = filename;
            dataDropdown.appendChild(opt);
            dataDropdown.value = path;
        }
    }

    function selectDataByPath(data, displayName, startQ, endQ) {
        if (!data) return;
        currentData = data;
        if (Array.isArray(data)) {
            if (dataPathInput) dataPathInput.value = data.length > 1 ? `${data.length} files selected` : data[0].split('\\').pop().split('/').pop();
        } else {
            const filename = displayName || data.split('\\').pop().split('/').pop();
            if (dataPathInput) dataPathInput.value = filename;
        }
        setDataDropdownValue(data);
        updateSymbolPill();
        loadDatasetQuarters(data, startQ, endQ);
    }

    function loadLocalData(autoSelectFirst = true) {
        if (!dataDropdown) return Promise.resolve();
        dataDropdown.innerHTML = '<option value="">Scanning Market_Data...</option>';
        return fetch('/api/local_data')
            .then(r => r.json())
            .then(data => {
                availableDatasets = data.datasets || [];
                dataDropdown.innerHTML = '';

                if (availableDatasets.length === 0) {
                    const opt = document.createElement('option');
                    opt.value = '';
                    opt.textContent = '(No datasets in Market_Data)';
                    dataDropdown.appendChild(opt);
                } else {
                    availableDatasets.forEach(d => {
                        const opt = document.createElement('option');
                        opt.value = d.path;
                        opt.textContent = d.display_name;
                        dataDropdown.appendChild(opt);
                    });
                }

                if (currentData) {
                    setDataDropdownValue(currentData);
                } else if (autoSelectFirst && availableDatasets.length > 0) {
                    selectDataByPath(availableDatasets[0].path, availableDatasets[0].display_name);
                }
            })
            .catch(e => {
                console.error("Error loading market data:", e);
                dataDropdown.innerHTML = '<option value="">Error loading data</option>';
            });
    }

    // Startup discovery
    loadLocalStrategies(true);
    loadLocalData(true);

    // Strategy Dropdown change handler
    if (strategyDropdown) {
        strategyDropdown.addEventListener('change', () => {
            const val = strategyDropdown.value;
            if (val) {
                const selectedText = strategyDropdown.options[strategyDropdown.selectedIndex].text;
                selectStrategyByPath(val, selectedText);
            }
        });
    }

    // Data Dropdown change handler
    if (dataDropdown) {
        dataDropdown.addEventListener('change', () => {
            const val = dataDropdown.value;
            if (val && val !== '__multi__') {
                const selectedText = dataDropdown.options[dataDropdown.selectedIndex].text;
                selectDataByPath(val, selectedText);
            }
        });
    }

    // Refresh Buttons
    if (btnRefreshStrategies) {
        btnRefreshStrategies.addEventListener('click', () => {
            setStatus('Scanning Strategy_Files folders...');
            loadLocalStrategies(false).then(() => setStatus('Strategies updated', 'var(--success)'));
        });
    }

    if (btnRefreshData) {
        btnRefreshData.addEventListener('click', () => {
            setStatus('Scanning Market_Data folders...');
            loadLocalData(false).then(() => setStatus('Market data updated', 'var(--success)'));
        });
    }

    // Quarter Slider Functions
    function formatQuarterDisplay(qStr) {
        if (!qStr) return '--';
        const parts = qStr.replace('(', '').replace(')', '').replace(' ', '-').split('-');
        if (parts.length >= 2) {
            const yr = parts[0];
            const q = parts[1].toUpperCase().includes('Q') ? parts[1].toUpperCase() : 'Q' + parts[1];
            return `${yr} (${q})`;
        }
        return qStr;
    }

    function updateQuarterSliderUI() {
        if (!availableQuarters || availableQuarters.length === 0) return;
        const total = availableQuarters.length - 1;
        let startIdx = parseInt(quarterStartRange.value) || 0;
        let endIdx = parseInt(quarterEndRange.value) || 0;

        if (startIdx > endIdx) {
            startIdx = endIdx;
            quarterStartRange.value = startIdx;
        }

        selectedStartQuarter = availableQuarters[startIdx];
        selectedEndQuarter = availableQuarters[endIdx];

        if (startQuarterLabel) startQuarterLabel.textContent = formatQuarterDisplay(selectedStartQuarter);
        if (endQuarterLabel) endQuarterLabel.textContent = formatQuarterDisplay(selectedEndQuarter);

        const leftPct = total > 0 ? (startIdx / total) * 100 : 0;
        const rightPct = total > 0 ? (1 - (endIdx / total)) * 100 : 0;

        if (quarterSliderHighlight) {
            quarterSliderHighlight.style.left = `${leftPct}%`;
            quarterSliderHighlight.style.right = `${rightPct}%`;
        }

        const count = (endIdx - startIdx) + 1;
        const years = (count / 4).toFixed(1);
        if (quarterCountBadge) {
            quarterCountBadge.textContent = `${count} Qtrs (${years} Yrs)`;
        }
    }

    function setQuarterSliderRange(startQ, endQ) {
        if (!availableQuarters || availableQuarters.length === 0) return;
        let startIdx = 0;
        let endIdx = availableQuarters.length - 1;

        if (startQ) {
            const idx = availableQuarters.indexOf(startQ);
            if (idx !== -1) startIdx = idx;
        }
        if (endQ) {
            const idx = availableQuarters.indexOf(endQ);
            if (idx !== -1) endIdx = idx;
        }

        if (quarterStartRange) quarterStartRange.value = startIdx;
        if (quarterEndRange) quarterEndRange.value = endIdx;
        updateQuarterSliderUI();
    }

    function loadDatasetQuarters(dataPath, defaultStartQ, defaultEndQ) {
        if (!dataPath) return Promise.resolve();
        return fetch(`/api/data_quarters?data=${encodeURIComponent(typeof dataPath === 'string' ? dataPath : JSON.stringify(dataPath))}`)
            .then(r => r.json())
            .then(data => {
                availableQuarters = data.quarters || [];
                if (availableQuarters.length > 0) {
                    const total = availableQuarters.length - 1;
                    if (quarterStartRange) {
                        quarterStartRange.min = 0;
                        quarterStartRange.max = total;
                    }
                    if (quarterEndRange) {
                        quarterEndRange.min = 0;
                        quarterEndRange.max = total;
                    }
                    if (sliderMinLabel) sliderMinLabel.textContent = formatQuarterDisplay(availableQuarters[0]);
                    if (sliderMaxLabel) sliderMaxLabel.textContent = formatQuarterDisplay(availableQuarters[total]);

                    setQuarterSliderRange(defaultStartQ || availableQuarters[0], defaultEndQ || availableQuarters[total]);
                }
            })
            .catch(e => console.error("Error loading dataset quarters:", e));
    }

    if (quarterStartRange) {
        quarterStartRange.addEventListener('input', () => {
            let startIdx = parseInt(quarterStartRange.value) || 0;
            let endIdx = parseInt(quarterEndRange.value) || 0;
            if (startIdx > endIdx) {
                quarterEndRange.value = startIdx;
            }
            updateQuarterSliderUI();
        });
    }

    if (quarterEndRange) {
        quarterEndRange.addEventListener('input', () => {
            let startIdx = parseInt(quarterStartRange.value) || 0;
            let endIdx = parseInt(quarterEndRange.value) || 0;
            if (endIdx < startIdx) {
                quarterStartRange.value = endIdx;
            }
            updateQuarterSliderUI();
        });
    }

    function loadParams(scriptPath) {
        setStatus('Loading parameters...');
        fetch(`/api/params?script=${encodeURIComponent(scriptPath)}`).then(r => r.json()).then(params => {
            if (params.error) {
                paramsContainer.innerHTML = '<h3>Parameters</h3><p class="empty-text" style="color:var(--danger)">Error loading params.</p>';
                setStatus('Error loading params', 'var(--danger)');
                return;
            }
            paramsContainer.innerHTML = '<h3>Parameters</h3>';
            const optInputTbody = document.querySelector('#optimizationInputTable tbody');
            if (optInputTbody) optInputTbody.innerHTML = '';

            const wfoInputTbody = document.querySelector('#wfoInputTable tbody');
            if (wfoInputTbody) wfoInputTbody.innerHTML = '';

            Object.keys(params).forEach(k => {
                let val = params[k];
                if (k === 'RISK_TYPE ($ or %)') {
                    let isDollar = val === '$' ? 'active' : '';
                    let isPct = val === '%' ? 'active' : '';
                    paramsContainer.innerHTML += `
                        <div class="param-item">
                            <label>${k}</label>
                            <div style="display: flex; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; background: var(--bg-color); height: 26px; width: 100%;">
                                <div class="risk-type-btn ${isDollar}" data-val="$" style="flex:1; text-align:center; padding: 2px 0; cursor: pointer; border-right: 1px solid var(--border); ${isDollar ? 'background: var(--accent); color: white;' : ''}">$</div>
                                <div class="risk-type-btn ${isPct}" data-val="%" style="flex:1; text-align:center; padding: 2px 0; cursor: pointer; ${isPct ? 'background: var(--accent); color: white;' : ''}">%</div>
                            </div>
                            <input type="hidden" data-key="${k}" value="${val}" id="riskTypeInput">
                        </div>
                    `;
                } else {
                    let isNum = typeof val === 'number';
                    paramsContainer.innerHTML += `
                        <div class="param-item">
                            <label>${k}</label>
                            <input type="${isNum ? 'number' : 'text'}" step="any" data-key="${k}" value="${val}">
                        </div>
                    `;

                    if (isNum) {
                        if (optInputTbody) {
                            optInputTbody.innerHTML += `
                                <tr data-param="${k}">
                                    <td style="text-align: center;"><input type="checkbox" class="opt-check"></td>
                                    <td>${k}</td>
                                    <td>${val}</td>
                                    <td><input type="number" class="opt-start" value="${val}" step="any"></td>
                                    <td><input type="number" class="opt-end" value="${val}" step="any"></td>
                                    <td><input type="number" class="opt-step" value="0" step="any"></td>
                                    <td class="opt-step-count" style="text-align: center;">1</td>
                                </tr>
                            `;
                        }

                        if (wfoInputTbody) {
                            const kUpper = k.toUpperCase();
                            const isExcludedFromDefault = kUpper.includes('COMMISSION') || kUpper.includes('TICK') || kUpper.includes('POINT') || kUpper.includes('RATE') || kUpper.includes('RISK') || kUpper.includes('MAX_TRADES');
                            const checkedAttr = !isExcludedFromDefault ? 'checked' : '';
                            const isInt = Number.isInteger(val);
                            const defaultStep = isInt ? 1 : 0.1;
                            wfoInputTbody.innerHTML += `
                                <tr data-param="${k}">
                                    <td style="text-align: center;"><input type="checkbox" class="wfo-check" ${checkedAttr}></td>
                                    <td><strong>${k}</strong></td>
                                    <td>${val}</td>
                                    <td><input type="number" class="wfo-start" value="${val}" step="any" style="width:75px; padding:4px; background:var(--bg-tertiary); border:1px solid var(--border-color); color:var(--text-primary); border-radius:4px;"></td>
                                    <td><input type="number" class="wfo-end" value="${val}" step="any" style="width:75px; padding:4px; background:var(--bg-tertiary); border:1px solid var(--border-color); color:var(--text-primary); border-radius:4px;"></td>
                                    <td><input type="number" class="wfo-step" value="${defaultStep}" step="any" style="width:65px; padding:4px; background:var(--bg-tertiary); border:1px solid var(--border-color); color:var(--text-primary); border-radius:4px;"></td>
                                    <td class="wfo-step-count" style="text-align: center; font-family: monospace;">1</td>
                                </tr>
                            `;
                        }
                    }
                }
            });

            attachOptimizationListeners();
            attachWFOListeners();

            document.querySelectorAll('.risk-type-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.risk-type-btn').forEach(b => {
                        b.classList.remove('active');
                        b.style.background = '';
                        b.style.color = '';
                    });
                    const target = e.currentTarget;
                    target.classList.add('active');
                    target.style.background = 'var(--accent)';
                    target.style.color = 'white';
                    const selectedVal = target.getAttribute('data-val');
                    document.getElementById('riskTypeInput').value = selectedVal;

                    const riskValueInput = document.querySelector('input[data-key="RISK_VALUE"]');
                    if (riskValueInput) {
                        if (selectedVal === '%') {
                            riskValueInput.value = 1.0;
                        } else if (selectedVal === '$') {
                            riskValueInput.value = 1000.0;
                        }
                    }
                });
            });

            setStatus('Ready');
        });
    }

    function getParamsFromUI() {
        const params = {};
        document.querySelectorAll('.param-item input').forEach(input => {
            if (input.type === 'number') {
                params[input.getAttribute('data-key')] = parseFloat(input.value);
            } else {
                params[input.getAttribute('data-key')] = input.value;
            }
        });
        return params;
    }

    // Run Backtest
    btnRun.addEventListener('click', () => {
        const script = currentScript;
        const data = currentData;
        if (!script || !data) return alert("Select script and data");

        const origBtnText = btnRun.innerText;
        btnRun.innerText = 'Running...';
        btnRun.disabled = true;
        const payload = { 
            script, 
            data, 
            start_quarter: selectedStartQuarter, 
            end_quarter: selectedEndQuarter, 
            params: getParamsFromUI() 
        };

        fetch('/api/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(r => r.json()).then(res => {
            btnRun.innerText = origBtnText;
            btnRun.disabled = false;
            if (res.error) {
                alert("Error running backtest: " + res.error);
                return;
            }
            mainContent.style.display = 'flex';

            // Hide the sidebar automatically
            const sidebar = document.getElementById('sidebar');
            if (sidebar) sidebar.style.display = 'none';
            window.dispatchEvent(new Event('resize'));

            renderResults(res);
            // removed setStatus


            // Clear Robustness Table
            robustnessTable.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-secondary);">Click "Run Stress Test" to calculate.</td></tr>';

            // Clear Monte Carlo
            document.getElementById('mcStatsGrid').innerHTML = '';
            document.getElementById('plotMonteCarlo').style.display = 'none';
            document.getElementById('plotDistribution').style.display = 'none';
            lastMCResults = null;

        }).catch(err => {
            btnRun.innerText = origBtnText;
            btnRun.disabled = false;
        });
    });

    if (btnRunRobustness) {
        btnRunRobustness.addEventListener('click', () => {
            const script = currentScript;
            const data = currentData;
            if (!script || !data) return alert("Select script and data first.");
            const shiftPctEl = document.getElementById('robustnessShiftPct');
            const shift_pct = shiftPctEl ? (parseFloat(shiftPctEl.value) || 25) : 25;
            const payload = { 
                script, 
                data, 
                start_quarter: selectedStartQuarter, 
                end_quarter: selectedEndQuarter, 
                params: getParamsFromUI(), 
                shift_pct 
            };
            runRobustness(payload);
        });
    }

    function runRobustness(payload) {
        const btnRunRobustness = document.getElementById('btnRunRobustness');
        const origBtnText = btnRunRobustness.innerText;
        btnRunRobustness.innerText = 'Running...';
        btnRunRobustness.disabled = true;

        robustnessTable.innerHTML = '<tr><td colspan="7">Running 25% Stress Test...</td></tr>';
        fetch('/api/robustness', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(r => r.json()).then(res => {
            const btnRunRobustness = document.getElementById('btnRunRobustness');
            btnRunRobustness.innerText = 'Run Stress Test';
            btnRunRobustness.disabled = false;
            if (res.error) {
                robustnessTable.innerHTML = `<tr><td colspan="7" style="color:red;">Error: ${res.error}</td></tr>`;
                return;
            }
            robustnessTable.innerHTML = '';
            res.forEach(row => {
                let sig = row.significant ? '<span style="color:#34d399">Pass</span>' : '<span style="color:#fb7185">Fail</span>';

                let paramsStr = '';
                if (row.params) {
                    paramsStr = Object.entries(row.params)
                        .map(([k, v]) => {
                            const num = Number(v);
                            const valStr = (!isNaN(num) && typeof v !== 'boolean' && v !== '' && v !== null)
                                ? (Number.isInteger(num) ? num : num.toFixed(2))
                                : v;
                            return `${k}:${valStr}`;
                        })
                        .join(', ');
                }

                robustnessTable.innerHTML += `
                    <tr>
                        <td><strong>${row.variation}</strong><br><span style="font-size:0.75em;color:var(--text-secondary);">${paramsStr}</span></td>
                        <td>${row.metrics && row.metrics['CAGR (%)'] !== undefined ? row.metrics['CAGR (%)'] + '%' : '-%'}</td>
                        <td>${row.metrics && row.metrics['Max DD (%)'] !== undefined ? (-row.metrics['Max DD (%)']).toFixed(2) + '%' : '-%'}</td>
                        <td>${row.metrics && row.metrics['Calmar Ratio'] !== undefined ? row.metrics['Calmar Ratio'] : '-'}</td>
                        <td>${row.metrics && row.metrics['Profit Factor'] !== undefined ? row.metrics['Profit Factor'] : '-'}</td>
                        <td>${row.metrics && row.metrics['Sharpe'] !== undefined ? row.metrics['Sharpe'] : '-'}</td>
                        <td>${row.metrics && row.metrics['Sortino'] !== undefined ? row.metrics['Sortino'] : '-'}</td>
                        <td>${row.p_value !== null && row.p_value !== undefined ? (row.p_value * 100).toFixed(2) + '%' : '-'} (${sig})</td>
                    </tr>
                `;
            });
        }).catch(err => {
            const btnRunRobustness = document.getElementById('btnRunRobustness');
            btnRunRobustness.innerText = 'Run Stress Test';
            btnRunRobustness.disabled = false;
            robustnessTable.innerHTML = '<tr><td colspan="7" style="color:var(--danger);">Failed to load robustness tests.</td></tr>';
        });
    }

    function renderResults(data) {
        // 1. Top Metrics Grid
        metricsGrid.innerHTML = '';
        const m = data.metrics;

        function getMetricColor(k, val) {
            if (val === 'N/A' || val === null || val === undefined) return '';
            if (k === 'P-Value') return parseFloat(val) <= 0.05 ? 'positive' : 'negative';
            if (k.includes('Drawdown') || k.includes('DD')) return 'negative';
            if (k.includes('Profit Factor')) return val >= 1.0 ? 'positive' : 'negative';
            if (k.includes('Trades')) return '';
            return parseFloat(val) >= 0 ? 'positive' : 'negative';
        }

        // Maps our semantic color classes onto the tighter kpi/panel markup.
        // getMetricColor returns 'positive'/'negative'/''; we translate that
        // (plus a couple of hand-picked cases) into positive/negative/cyan/yellow.
        function colorClassFor(k, val, override) {
            if (override) return override;
            const base = getMetricColor(k, val);
            return base === 'positive' ? 'positive' : base === 'negative' ? 'negative' : '';
        }

        function kpiCell(label, k, tooltip, colorOverride) {
            const val = m[k];
            const cls = colorClassFor(k, val, colorOverride);
            return `
                <div class="kpi-cell" title="${tooltip}">
                    <div class="kpi-label">${label}</div>
                    <div class="kpi-value ${cls}">${formatValue(k, val)}</div>
                </div>
            `;
        }

        function statLine(label, k, source) {
            let val;
            if (source === 'long') val = data.long_metrics ? data.long_metrics[k] : 0;
            else if (source === 'short') val = data.short_metrics ? data.short_metrics[k] : 0;
            else val = m[k];
            const cls = getMetricColor(k, val);
            return `<div class="stat-line"><span class="l">${label}</span><span class="v ${cls}">${formatValue(k, val)}</span></div>`;
        }

        const activeTabEl = document.querySelector('.sidebar-nav li.active');
        const isTabAll = !activeTabEl || activeTabEl.getAttribute('onclick').includes("'all'");
        metricsGrid.style.display = isTabAll ? 'block' : 'none';
        metricsGrid.style.flex = 'none';

        const longTrades = data.long_metrics ? data.long_metrics['Trades'] : 0;
        const shortTrades = data.short_metrics ? data.short_metrics['Trades'] : 0;
        const totalTrades = (longTrades || 0) + (shortTrades || 0);
        const longPct = totalTrades > 0 ? Math.round((longTrades / totalTrades) * 100) : 0;
        const shortPct = 100 - longPct;

        const lProf = data.long_metrics && data.long_metrics['Net Profit'] !== undefined ? data.long_metrics['Net Profit'] : 0;
        const sProf = data.short_metrics && data.short_metrics['Net Profit'] !== undefined ? data.short_metrics['Net Profit'] : 0;
        let longProfitPct = 50;
        const lPos = Math.max(0, lProf);
        const sPos = Math.max(0, sProf);
        if (lPos > 0 || sPos > 0) {
            longProfitPct = Math.round((lPos / (lPos + sPos)) * 100);
        } else if (lProf < 0 || sProf < 0) {
            const lNeg = Math.abs(lProf);
            const sNeg = Math.abs(sProf);
            longProfitPct = Math.round((lNeg / (lNeg + sNeg)) * 100);
        }
        const shortProfitPct = 100 - longProfitPct;

        const lPf = data.long_metrics && data.long_metrics['Profit Factor'] !== undefined ? data.long_metrics['Profit Factor'] : 0;
        const sPf = data.short_metrics && data.short_metrics['Profit Factor'] !== undefined ? data.short_metrics['Profit Factor'] : 0;
        let longPfPct = 50;
        const lPfPos = Math.max(0, lPf);
        const sPfPos = Math.max(0, sPf);
        if (lPfPos > 0 || sPfPos > 0) {
            longPfPct = Math.round((lPfPos / (lPfPos + sPfPos)) * 100);
        }
        const shortPfPct = 100 - longPfPct;

        metricsGrid.innerHTML = `
            <div class="kpi-row">
                ${kpiCell('RETURN', 'Return (%)', 'Net absolute return over the full backtest')}
                ${kpiCell('CAGR', 'CAGR (%)', 'Compound annual growth rate')}
                ${kpiCell('MAX DD', 'Max DD (%)', 'Peak-to-trough drawdown', 'negative')}
                ${kpiCell('CALMAR', 'Calmar Ratio', 'CAGR / Max DD', 'cyan')}
                ${kpiCell('P-VALUE', 'P-Value', 'Statistical significance of the edge (lower is stronger)', 'yellow')}
                ${kpiCell('WIN RATE', 'Winning (%)', 'Winning trade ratio', '')}
                <div class="kpi-cell tail">
                    <div class="tail-label-row">
                        <span class="kpi-label">BOTTOM</span>
                        <div class="tail-toggle" id="tailToggle">
                            <button data-val="1" class="active">99</button>
                            <button data-val="5">95</button>
                            <button data-val="10">90</button>
                        </div>
                    </div>
                    <div class="tail-sublabel">% Net Return</div>
                    <div class="kpi-value ${getMetricColor('Top 1%', m['Top 1%'])}" id="topReturnValue">${formatValue('Return (%)', m['Top 1%'])}</div>
                </div>
            </div>

            <div class="panel-row">
                <div class="panel-card">
                    <div class="panel-head"><span class="dot" style="background:var(--cyan)"></span>Trade Execution</div>
                    <div class="stat-box"><div class="stat-label">TOTAL TRADES</div><div class="stat-value">${formatValue('Trades', m['Trades'])}</div></div>
                    ${statLine('Long', 'Trades', 'long')}
                    ${statLine('Short', 'Trades', 'short')}
                    <div class="split-caption"><span>Long ${longPct}%</span><span>Short ${shortPct}%</span></div>
                    <div class="split-bar"><span style="width:${longPct}%;"></span></div>
                </div>

                <div class="panel-card">
                    <div class="panel-head"><span class="dot" style="background:var(--green)"></span>Total Profit</div>
                    <div class="stat-box"><div class="stat-label">OVERALL</div><div class="stat-value ${getMetricColor('Net Profit', m['Net Profit'])}">${formatValue('Net Profit', m['Net Profit'])}</div></div>
                    ${statLine('Long', 'Net Profit', 'long')}
                    ${statLine('Short', 'Net Profit', 'short')}
                    <div class="split-caption"><span>Long ${longProfitPct}%</span><span>Short ${shortProfitPct}%</span></div>
                    <div class="split-bar"><span style="width:${longProfitPct}%;"></span></div>
                </div>

                <div class="panel-card">
                    <div class="panel-head"><span class="dot" style="background:var(--success)"></span>Profit Factor</div>
                    <div class="stat-box"><div class="stat-label">OVERALL</div><div class="stat-value ${getMetricColor('Profit Factor', m['Profit Factor'])}">${formatValue('Profit Factor', m['Profit Factor'])}<span class="sub">${m['Profit Factor'] >= 1.0 ? 'stable' : 'weak'}</span></div></div>
                    ${statLine('Long', 'Profit Factor', 'long')}
                    ${statLine('Short', 'Profit Factor', 'short')}
                    <div class="split-caption"><span>Long ${longPfPct}%</span><span>Short ${shortPfPct}%</span></div>
                    <div class="split-bar"><span style="width:${longPfPct}%;"></span></div>
                </div>

                <div class="panel-card">
                    <div class="panel-head"><span class="dot" style="background:var(--yellow)"></span>Risk Adjusted</div>
                    <div class="ratio-row"><span class="name">Sortino</span><span class="big" style="color:var(--cyan)">${formatValue('Sortino', m['Sortino'])}</span></div>
                    <div class="ratio-split"><span>L: <b>${formatValue('Sortino', data.long_metrics ? data.long_metrics['Sortino'] : 0)}</b></span><span>S: <b>${formatValue('Sortino', data.short_metrics ? data.short_metrics['Sortino'] : 0)}</b></span></div>
                    <div class="ratio-row"><span class="name">Sharpe</span><span class="big" style="color:var(--yellow)">${formatValue('Sharpe', m['Sharpe'])}</span></div>
                    <div class="ratio-split"><span>L: <b>${formatValue('Sharpe', data.long_metrics ? data.long_metrics['Sharpe'] : 0)}</b></span><span>S: <b>${formatValue('Sharpe', data.short_metrics ? data.short_metrics['Sharpe'] : 0)}</b></span></div>
                    <div class="ratio-row"><span class="name">K-Ratio</span><span class="big" style="color:var(--text-primary)">${formatValue('K-Ratio', m['K-Ratio'])}</span></div>
                    <div class="ratio-split"><span>L: <b>${formatValue('K-Ratio', data.long_metrics ? data.long_metrics['K-Ratio'] : 0)}</b></span><span>S: <b>${formatValue('K-Ratio', data.short_metrics ? data.short_metrics['K-Ratio'] : 0)}</b></span></div>
                </div>
            </div>
        `;

        updateSymbolPill();

        // Tail-percentile toggle (99 / 95 / 90)
        setTimeout(() => {
            const btns = document.querySelectorAll('#tailToggle button');
            btns.forEach(btn => {
                btn.addEventListener('click', (e) => {
                    btns.forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');

                    const key = 'Top ' + e.target.getAttribute('data-val') + '%';
                    const val = m[key];
                    const valEl = document.getElementById('topReturnValue');
                    valEl.className = 'kpi-value ' + getMetricColor(key, val);
                    valEl.innerText = formatValue('Return (%)', val);
                });
            });
        }, 0);

        // Store last backtest data globally for responsive tab switches
        window.lastBacktestData = data;

        // 2. Plotly Equity Curves
        lastEquityAll = data.equity_curve || [];
        lastEquityLong = data.long_equity_curve || [];
        lastEquityShort = data.short_equity_curve || [];

        if (btnShowAll) {
            btnShowAll.classList.add('primary');
            btnShowLong.classList.remove('primary');
            btnShowShort.classList.remove('primary');
        }

        plotEquity('plotEquityMain', lastEquityAll, '#1a8c54', 'All Trades');

        // 3. Heatmap
        lastHeatmapAll = data.monthly_heatmap || [];
        lastHeatmapLong = data.long_monthly_heatmap || [];
        lastHeatmapShort = data.short_monthly_heatmap || [];

        const btnHeatmapAll = document.getElementById('btnHeatmapAll');
        const btnHeatmapLong = document.getElementById('btnHeatmapLong');
        const btnHeatmapShort = document.getElementById('btnHeatmapShort');

        if (btnHeatmapAll) {
            btnHeatmapAll.classList.add('primary');
            btnHeatmapLong.classList.remove('primary');
            btnHeatmapShort.classList.remove('primary');
        }

        if (data.monthly_heatmap) {
            renderHeatmap(lastHeatmapAll);
        }

        // 4. Matrix
        renderMatrix(data.metrics, data.long_metrics, data.short_metrics);

        // 5. Best and Worst Moments
        if (data.best_worst_moments) {
            window.lastBestWorst = data.best_worst_moments;
            renderBestWorst(window.lastBestWorst, 'Days');

            const btnBwDays = document.getElementById('btnBwDays');
            const btnBwWeeks = document.getElementById('btnBwWeeks');
            const btnBwMonths = document.getElementById('btnBwMonths');

            if (btnBwDays) {
                btnBwDays.onclick = () => {
                    btnBwDays.classList.add('primary');
                    btnBwWeeks.classList.remove('primary');
                    btnBwMonths.classList.remove('primary');
                    renderBestWorst(window.lastBestWorst, 'Days');
                };
                btnBwWeeks.onclick = () => {
                    btnBwWeeks.classList.add('primary');
                    btnBwDays.classList.remove('primary');
                    btnBwMonths.classList.remove('primary');
                    renderBestWorst(window.lastBestWorst, 'Weeks');
                };
                btnBwMonths.onclick = () => {
                    btnBwMonths.classList.add('primary');
                    btnBwDays.classList.remove('primary');
                    btnBwWeeks.classList.remove('primary');
                    renderBestWorst(window.lastBestWorst, 'Months');
                };
            }
        }

        // 6. Drawdown Analysis
        if (data.dd_analysis_table && data.dd_analysis_curve) {
            renderDrawdownAnalysis(data.dd_analysis_table, data.dd_analysis_curve);
        }

        // 7. Trading Time Distribution
        if (data.trading_time_dist) {
            renderTradingTimeDist(data.trading_time_dist);
        }

        // 8. Day of Week Distribution
        if (data.dow_dist) {
            renderDayOfWeekDist(data.dow_dist);
        }

        // Expose global renderers
        window.renderMatrix = renderMatrix;
        window.renderTradingTimeDist = renderTradingTimeDist;
        window.renderDayOfWeekDist = renderDayOfWeekDist;
        window.renderBestWorst = renderBestWorst;
        window.renderDrawdownAnalysis = renderDrawdownAnalysis;
        window.renderHeatmap = renderHeatmap;
        window.plotEquity = plotEquity;

        // Trigger resize
        window.dispatchEvent(new Event('resize'));
    }

    function plotEquity(divId, curveData, color, name) {
        if (!curveData || !curveData.pct || curveData.pct.length === 0) {
            document.getElementById(divId).innerHTML = '<p class="empty-text">No data to display.</p>';
            return;
        }

        let fillColor = color === '#1a8c54' ? 'rgba(26, 140, 84, 0.25)' :
            color === '#3b82f6' ? 'rgba(59, 130, 246, 0.25)' : 'rgba(239, 68, 68, 0.25)';

        const x = Array.from({ length: curveData.pct.length }, (_, i) => i);

        const trace1 = {
            x: x, y: curveData.pct,
            customdata: curveData.dd,
            text: curveData.dates,
            fill: 'tozeroy', type: 'scatter', mode: 'lines',
            line: { color: color, width: 1.5 },
            fillcolor: fillColor,
            name: name + ' Return',
            hovertemplate: '<b>Date:</b> %{text}<br><b>Return:</b> %{y:.2f}%<br><b>Drawdown:</b> %{customdata:.2f}%<extra></extra>',
            yaxis: 'y'
        };

        const trace2 = {
            x: x, y: curveData.dd,
            customdata: curveData.pct,
            text: curveData.dates,
            fill: 'tozeroy', type: 'scatter', mode: 'lines',
            line: { color: 'rgba(239, 68, 68, 0.8)', width: 1.0 },
            fillcolor: 'rgba(239, 68, 68, 0.2)',
            name: 'Drawdown',
            hovertemplate: '<b>Date:</b> %{text}<br><b>Return:</b> %{customdata:.2f}%<br><b>Drawdown:</b> %{y:.2f}%<extra></extra>',
            yaxis: 'y2'
        };

        const layout = {
            plot_bgcolor: '#0e1117', paper_bgcolor: '#0e1117',
            autosize: true,
            font: { color: '#94a3b8' }, margin: { l: 60, r: 20, t: 20, b: 20 },
            xaxis: { showgrid: true, gridcolor: 'rgba(255,255,255,0.03)', zerolinecolor: 'rgba(255,255,255,0.03)', showticklabels: false },
            yaxis: { title: 'Return (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.03)', zerolinecolor: 'rgba(255,255,255,0.03)', domain: [0.35, 1] },
            yaxis2: { title: 'Drawdown (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.03)', zerolinecolor: 'rgba(255,255,255,0.03)', domain: [0, 0.25] },
            hovermode: 'x',
            hoverlabel: { bgcolor: '#1a1c23', font: { color: '#e2e8f0' }, bordercolor: '#333' },
            showlegend: false
        };

        plotlyRedraw(divId, [trace1, trace2], layout, { responsive: true });
    }

    // Handle Monte Carlo Generation
    if (btnRunMC) {
        btnRunMC.addEventListener('click', () => {
            const mcMethodEl = document.getElementById('mcMethod');
            const mcSimsEl = document.getElementById('mcSims');
            const mcPctEl = document.getElementById('mcPct');
            const mcRuinEl = document.getElementById('mcRuin');
            const payload = {
                method: mcMethodEl ? mcMethodEl.value : 'iid',
                num_simulations: mcSimsEl ? mcSimsEl.value : 1000,
                pct_trades: mcPctEl ? mcPctEl.value : 100,
                ruin_threshold: mcRuinEl ? mcRuinEl.value : 50
            };

            const origBtnText = btnRunMC.innerText;
            btnRunMC.innerText = 'Running...';
            btnRunMC.disabled = true;

            fetch('/api/run_monte_carlo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(res => {
                btnRunMC.innerText = origBtnText;
                btnRunMC.disabled = false;
                if (res.error) return alert(res.error);
                lastMCResults = res;
                renderMonteCarlo(res);
                const plotMC = document.getElementById('plotMonteCarlo');
                if (plotMC) plotMC.style.display = 'block';
                window.dispatchEvent(new Event('resize'));
            }).catch(err => {
                btnRunMC.innerText = origBtnText;
                btnRunMC.disabled = false;
            });
        });
    }

    function attachOptimizationListeners() {
        const table = document.getElementById('optimizationInputTable');
        if (!table) return;

        function updateStepCounts() {
            let totalCombos = 1;
            let anyChecked = false;

            table.querySelectorAll('tbody tr').forEach(row => {
                const isChecked = row.querySelector('.opt-check').checked;
                if (!isChecked) {
                    row.querySelector('.opt-step-count').innerText = "1";
                    return;
                }
                anyChecked = true;

                const start = parseFloat(row.querySelector('.opt-start').value) || 0;
                const end = parseFloat(row.querySelector('.opt-end').value) || 0;
                const step = parseFloat(row.querySelector('.opt-step').value) || 0;

                let count = 1;
                if (step > 0 && end >= start) {
                    count = Math.round((end - start) / step) + 1;
                }

                row.querySelector('.opt-step-count').innerText = count;
                totalCombos *= count;
            });

            if (!anyChecked) totalCombos = 0;
            document.getElementById('optTotalCombos').innerText = totalCombos.toLocaleString();
        }

        table.addEventListener('input', updateStepCounts);
        table.addEventListener('change', updateStepCounts);

        const selectAll = document.getElementById('optSelectAll');
        if (selectAll) {
            selectAll.addEventListener('change', (e) => {
                const checked = e.target.checked;
                table.querySelectorAll('.opt-check').forEach(cb => cb.checked = checked);
                updateStepCounts();
            });
        }
    }

    const btnRunOptimization = document.getElementById('btnRunOptimization');
    if (btnRunOptimization) {
        btnRunOptimization.addEventListener('click', () => {
            const script = currentScript;
            const data = currentData;
            if (!script || !data) return alert("Select script and data");

            const totalCombosStr = document.getElementById('optTotalCombos').innerText.replace(/,/g, '');
            const totalCombos = parseInt(totalCombosStr) || 0;

            if (totalCombos === 0) return alert("Select at least one parameter to optimize.");
            if (totalCombos > 5000) return alert(`Total combinations (${totalCombos}) exceeds the limit of 5000. Please reduce your ranges or increase steps.`);

            const optimizable_params = [];
            document.querySelectorAll('#optimizationInputTable tbody tr').forEach(row => {
                if (row.querySelector('.opt-check').checked) {
                    optimizable_params.push({
                        name: row.getAttribute('data-param'),
                        start: parseFloat(row.querySelector('.opt-start').value),
                        end: parseFloat(row.querySelector('.opt-end').value),
                        step: parseFloat(row.querySelector('.opt-step').value)
                    });
                }
            });

            btnRunOptimization.disabled = true;
            btnRunOptimization.innerText = `Running (0/${totalCombos})...`;

            const payload = { 
                script, 
                data, 
                start_quarter: selectedStartQuarter, 
                end_quarter: selectedEndQuarter, 
                base_params: getParamsFromUI(), 
                optimizable_params 
            };
            function getOptMetricVal(r, metricName) {
                if (!r) return NaN;
                if (metricName === 'P-Value (%)' || metricName === 'P-Value') {
                    if (r.p_value !== null && r.p_value !== undefined) return parseFloat((r.p_value * 100).toFixed(2));
                    if (r.metrics && r.metrics['P-Value'] !== undefined && r.metrics['P-Value'] !== null) return parseFloat((r.metrics['P-Value'] * 100).toFixed(2));
                    return NaN;
                }
                if (metricName === 'Calmar Ratio' || metricName === 'Calmar') {
                    if (r.metrics && r.metrics['Calmar Ratio'] !== undefined && r.metrics['Calmar Ratio'] !== null) return parseFloat(r.metrics['Calmar Ratio']);
                    if (r.metrics && r.metrics['CAGR (%)'] !== undefined && r.metrics['Max DD (%)'] !== undefined) {
                        const c = parseFloat(r.metrics['CAGR (%)']);
                        const d = Math.abs(parseFloat(r.metrics['Max DD (%)']));
                        return d > 0 ? parseFloat((c / d).toFixed(2)) : 0;
                    }
                    return NaN;
                }
                if (!r.metrics) return NaN;
                let val = r.metrics[metricName];
                if (val === undefined || val === null) return NaN;
                if (typeof val === 'string' && val.includes('%')) return parseFloat(val.replace('%', ''));
                return parseFloat(val);
            }

            function getOptSortVal(row, col) {
                if (!row) return -999999;
                if (col === 'P-Value' || col === 'P-Value (%)') {
                    return (row.p_value !== null && row.p_value !== undefined) ? parseFloat(row.p_value) : -999999;
                }
                if (col === 'Calmar Ratio' || col === 'Calmar') {
                    const cVal = getOptMetricVal(row, 'Calmar Ratio');
                    return isNaN(cVal) ? -999999 : cVal;
                }
                if (row.metrics && row.metrics[col] !== undefined && row.metrics[col] !== null) {
                    let v = row.metrics[col];
                    if (typeof v === 'string' && v.includes('%')) return parseFloat(v.replace('%', ''));
                    return parseFloat(v);
                }
                if (row.param_combo && row.param_combo[col] !== undefined) {
                    return parseFloat(row.param_combo[col]);
                }
                return -999999;
            }

            function renderOptResults(data, optParams) {
                const tableBody = document.querySelector('#optimizationResultsTable tbody');
                tableBody.innerHTML = '';

                if (window.optSortCol) {
                    data.sort((a, b) => {
                        let valA = getOptSortVal(a, window.optSortCol);
                        let valB = getOptSortVal(b, window.optSortCol);

                        if (window.optSortCol === 'Max DD (%)') {
                            valA = -valA;
                            valB = -valB;
                        }

                        if (valA < valB) return window.optSortAsc ? -1 : 1;
                        if (valA > valB) return window.optSortAsc ? 1 : -1;
                        return 0;
                    });
                }

                let html = '';
                data.forEach((row, index) => {
                    let paramColsHtml = '';
                    optParams.forEach(p => {
                        paramColsHtml += `<td><strong>${row.param_combo[p.name]}</strong></td>`;
                    });

                    html += `
                        <tr>
                            <td>${index + 1}</td>
                            ${paramColsHtml}
                            <td>${row.metrics && row.metrics['Trades'] !== undefined ? row.metrics['Trades'] : '-'}</td>
                            <td>${row.metrics && row.metrics['CAGR (%)'] !== undefined ? row.metrics['CAGR (%)'] : '-'}%</td>
                            <td>${row.metrics && row.metrics['Max DD (%)'] !== undefined ? (-row.metrics['Max DD (%)']).toFixed(2) : '-'}%</td>
                            <td>${row.metrics && row.metrics['Calmar Ratio'] !== undefined && row.metrics['Calmar Ratio'] !== null ? row.metrics['Calmar Ratio'] : '-'}</td>
                            <td>${row.metrics && row.metrics['Profit Factor'] !== undefined && row.metrics['Profit Factor'] !== null ? row.metrics['Profit Factor'] : '-'}</td>
                            <td>${row.metrics && row.metrics['Sharpe'] !== undefined ? row.metrics['Sharpe'] : '-'}</td>
                            <td>${row.metrics && row.metrics['Sortino'] !== undefined ? row.metrics['Sortino'] : '-'}</td>
                            <td>${row.p_value !== null && row.p_value !== undefined ? (row.p_value * 100).toFixed(2) + '%' : '-'}</td>
                        </tr>
                    `;
                });
                tableBody.innerHTML = html;
            }

            fetch('/api/optimize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(async response => {
                const reader = response.body.getReader();
                const decoder = new TextDecoder("utf-8");
                let buffer = "";

                const tableHeaders = document.getElementById('optimizationResultsHeaders');

                let paramHeadersHtml = '';
                optimizable_params.forEach(p => {
                    paramHeadersHtml += `<th class="sortable" data-sort="${p.name}" style="cursor:pointer; user-select:none;">${p.name} <span></span></th>`;
                });

                tableHeaders.innerHTML = `<th>S.No.</th>` + paramHeadersHtml + `
                    <th class="sortable" data-sort="Trades" style="cursor:pointer; user-select:none;">Total Trades <span></span></th>
                    <th class="sortable" data-sort="CAGR (%)" style="cursor:pointer; user-select:none;">CAGR (%) <span></span></th>
                    <th class="sortable" data-sort="Max DD (%)" style="cursor:pointer; user-select:none;">Max DD (%) <span></span></th>
                    <th class="sortable" data-sort="Calmar Ratio" style="cursor:pointer; user-select:none;">Calmar Ratio <span></span></th>
                    <th class="sortable" data-sort="Profit Factor" style="cursor:pointer; user-select:none;">Profit Factor <span></span></th>
                    <th class="sortable" data-sort="Sharpe" style="cursor:pointer; user-select:none;">Sharpe Ratio <span></span></th>
                    <th class="sortable" data-sort="Sortino" style="cursor:pointer; user-select:none;">Sortino Ratio <span></span></th>
                    <th class="sortable" data-sort="P-Value" style="cursor:pointer; user-select:none;">P-Value <span></span></th>
                `;

                window.optResultsData = [];
                window.optParams = optimizable_params;
                window.optSortCol = null;
                window.optSortAsc = false;

                document.querySelectorAll('#optimizationResultsHeaders th.sortable').forEach(th => {
                    th.addEventListener('click', () => {
                        const col = th.getAttribute('data-sort');
                        if (window.optSortCol === col) {
                            window.optSortAsc = !window.optSortAsc;
                        } else {
                            window.optSortCol = col;
                            window.optSortAsc = false;
                        }

                        document.querySelectorAll('#optimizationResultsHeaders th.sortable span').forEach(s => s.innerText = '');
                        const span = th.querySelector('span');
                        if (span) span.innerText = window.optSortAsc ? '▲' : '▼';

                        renderOptResults(window.optResultsData, optimizable_params);
                    });
                });

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split("\n");
                    buffer = lines.pop(); // Keep incomplete line

                    for (const line of lines) {
                        if (!line.trim()) continue;
                        try {
                            const result = JSON.parse(line);
                            if (result.error) {
                                console.warn("Optimization error for combo:", result.error);
                            } else {
                                window.optResultsData.push(result);
                                const progressPct = Math.round((result._iteration / result._total) * 100);
                                btnRunOptimization.innerText = `Optimizing (${result._iteration}/${result._total} - ${progressPct}%)`;

                                renderOptResults(window.optResultsData, optimizable_params);
                                updateOptimizationSummary();
                            }
                        } catch (err) {
                            console.error("Failed to parse chunk", line, err);
                        }
                    }
                }

                btnRunOptimization.disabled = false;
                btnRunOptimization.innerText = 'Run Optimization';
            }).catch(e => {
                btnRunOptimization.disabled = false;
                btnRunOptimization.innerText = 'Run Optimization';
                alert('Error during optimization: ' + e.message);
            });
        });
    }

    function updateOptimizationSummary() {
        if (!window.optResultsData || window.optResultsData.length === 0) {
            document.getElementById('optSummaryPanel').style.display = 'none';
            document.getElementById('optHeatmapContainer').style.display = 'none';
            return;
        }

        document.getElementById('optSummaryPanel').style.display = 'block';

        const metric = document.getElementById('optSummaryMetric').value;
        const op = document.getElementById('optSummaryOperator').value;
        const threshold = parseFloat(document.getElementById('optSummaryThreshold').value);

        function getOptMetricVal(r, metricName) {
            if (!r) return NaN;
            if (metricName === 'P-Value (%)' || metricName === 'P-Value') {
                if (r.p_value !== null && r.p_value !== undefined) return parseFloat((r.p_value * 100).toFixed(2));
                if (r.metrics && r.metrics['P-Value'] !== undefined && r.metrics['P-Value'] !== null) return parseFloat((r.metrics['P-Value'] * 100).toFixed(2));
                return NaN;
            }
            if (metricName === 'Calmar Ratio' || metricName === 'Calmar') {
                if (r.metrics && r.metrics['Calmar Ratio'] !== undefined && r.metrics['Calmar Ratio'] !== null) return parseFloat(r.metrics['Calmar Ratio']);
                if (r.metrics && r.metrics['CAGR (%)'] !== undefined && r.metrics['Max DD (%)'] !== undefined) {
                    const c = parseFloat(r.metrics['CAGR (%)']);
                    const d = Math.abs(parseFloat(r.metrics['Max DD (%)']));
                    return d > 0 ? parseFloat((c / d).toFixed(2)) : 0;
                }
                return NaN;
            }
            if (!r.metrics) return NaN;
            let val = r.metrics[metricName];
            if (val === undefined || val === null) return NaN;
            if (typeof val === 'string' && val.includes('%')) return parseFloat(val.replace('%', ''));
            return parseFloat(val);
        }

        let passCount = 0;
        let validTotal = 0;
        window.optResultsData.forEach(row => {
            let val = getOptMetricVal(row, metric);
            if (isNaN(val)) return;
            validTotal++;

            let passed = false;
            if (op === '>' && val > threshold) passed = true;
            else if (op === '>=' && val >= threshold) passed = true;
            else if (op === '<' && val < threshold) passed = true;
            else if (op === '<=' && val <= threshold) passed = true;

            if (passed) passCount++;
        });

        const total = validTotal || window.optResultsData.length || 1;
        const pct = ((passCount / total) * 100).toFixed(1);
        const resEl = document.getElementById('optSummaryResult');
        resEl.innerText = `${passCount}/${total} (${pct}%)`;
        resEl.style.color = (passCount / total) >= 0.5 ? 'var(--success)' : (passCount / total) >= 0.2 ? 'var(--warning)' : 'var(--danger)';

        if (window.optParams && window.optParams.length === 2) {
            document.getElementById('optHeatmapContainer').style.display = 'block';
            const paramX = window.optParams[0].name;
            const paramY = window.optParams[1].name;

            const xSet = new Set();
            const ySet = new Set();
            window.optResultsData.forEach(r => {
                if (r.param_combo) {
                    xSet.add(r.param_combo[paramX]);
                    ySet.add(r.param_combo[paramY]);
                }
            });
            const xVals = Array.from(xSet).sort((a, b) => a - b);
            const yVals = Array.from(ySet).sort((a, b) => a - b);

            const zVals = Array(yVals.length).fill(null).map(() => Array(xVals.length).fill(null));

            window.optResultsData.forEach(r => {
                if (r.param_combo) {
                    const xIdx = xVals.indexOf(r.param_combo[paramX]);
                    const yIdx = yVals.indexOf(r.param_combo[paramY]);

                    let mVal = getOptMetricVal(r, metric);
                    if (xIdx !== -1 && yIdx !== -1 && !isNaN(mVal)) {
                        zVals[yIdx][xIdx] = mVal;
                    }
                }
            });

            const trace = {
                x: xVals,
                y: yVals,
                z: zVals,
                type: 'heatmap',
                colorscale: 'Viridis',
                colorbar: { title: metric, font: { color: '#e2e8f0' } }
            };

            const layout = {
                title: { text: `Optimization Heatmap (${paramX} vs ${paramY})`, font: { color: '#e2e8f0' } },
                xaxis: { title: paramX, tickfont: { color: '#94a3b8' }, titlefont: { color: '#e2e8f0' }, type: 'category' },
                yaxis: { title: paramY, tickfont: { color: '#94a3b8' }, titlefont: { color: '#e2e8f0' }, type: 'category' },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                font: { color: '#e2e8f0' },
                margin: { l: 60, r: 20, t: 40, b: 60 }
            };

            plotlyRedraw('optHeatmapContainer', [trace], layout, { displayModeBar: false });
        } else {
            document.getElementById('optHeatmapContainer').style.display = 'none';
        }
    }

    const optSummaryMetric = document.getElementById('optSummaryMetric');
    const optSummaryOperator = document.getElementById('optSummaryOperator');
    const optSummaryThreshold = document.getElementById('optSummaryThreshold');
    if (optSummaryMetric) optSummaryMetric.addEventListener('change', updateOptimizationSummary);
    if (optSummaryOperator) optSummaryOperator.addEventListener('change', updateOptimizationSummary);
    if (optSummaryThreshold) optSummaryThreshold.addEventListener('input', updateOptimizationSummary);

    function renderMonteCarlo(mc) {
        const mcStatsGrid = document.getElementById('mcStatsGrid');
        if (!mcStatsGrid) return;
        mcStatsGrid.innerHTML = '';

        const stats = [
            { k: 'Median Return', v: mc['Median Return'], c: mc['Median Return'] > 0 ? 'positive' : 'negative' },
            { k: '5th Pctl Return', v: mc['5th Percentile Return'], c: mc['5th Percentile Return'] > 0 ? 'positive' : 'negative' },
            { k: '95th Pctl Return', v: mc['95th Percentile Return'], c: mc['95th Percentile Return'] > 0 ? 'positive' : 'negative' },
            { k: 'Median Max DD', v: mc['Median Max Drawdown'], c: 'negative' },
            { k: '95th Pctl Max DD', v: mc['95th Percentile Max Drawdown'], c: 'negative' },
            { k: mc['Dispersion Metric Name'], v: mc['Dispersion Value'], c: 'positive' },
            { k: 'Ruin (≥50% DD)', v: mc['Probability of Ruin'], c: 'negative' }
        ];

        stats.forEach(s => {
            mcStatsGrid.innerHTML += `
                <div class="metric-card" style="padding: 10px; min-width: 130px;">
                    <h3 style="font-size: 13px; color: var(--text-secondary); margin-bottom: 5px;">${s.k}</h3>
                    <p class="${s.c}" style="font-size: 22px; font-weight: 500;">${s.v.toFixed(2)}%</p>
                </div>
            `;
        });

        const traces = [];
        const xLength = mc.original_curve ? mc.original_curve.length : 0;
        const x = Array.from({ length: xLength }, (_, i) => i);

        if (mc.bg_curves) {
            mc.bg_curves.forEach(curve => {
                traces.push({
                    x: x, y: curve, mode: 'lines',
                    line: { color: 'rgba(203, 213, 225, 0.35)', width: 1 },
                    hoverinfo: 'skip', showlegend: false
                });
            });
        }

        if (mc.weakest_curve) {
            traces.push({
                x: x, y: mc.weakest_curve, mode: 'lines', name: 'Weakest',
                line: { color: '#ef4444', width: 3 },
                hovertemplate: '%{y:.2f}%<extra></extra>'
            });
        }
        if (mc.average_curve) {
            traces.push({
                x: x, y: mc.average_curve, mode: 'lines', name: 'Average',
                line: { color: '#3b82f6', width: 3 },
                hovertemplate: '%{y:.2f}%<extra></extra>'
            });
        }
        if (mc.original_curve) {
            traces.push({
                x: x, y: mc.original_curve, mode: 'lines', name: 'Original',
                line: { color: '#22c55e', width: 3.5 },
                hovertemplate: '%{y:.2f}%<extra></extra>'
            });
        }

        const layout = {
            plot_bgcolor: 'rgba(0,0,0,0)', paper_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#e2e8f0' }, margin: { l: 50, r: 20, t: 30, b: 40 },
            xaxis: { title: 'Number of Trades', showgrid: true, gridcolor: 'rgba(255,255,255,0.05)' },
            yaxis: { title: 'Return (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.05)' },
            hovermode: 'x unified',
            hoverlabel: { bgcolor: '#1a1c23', font: { color: '#e2e8f0' }, bordercolor: '#333' },
            showlegend: true
        };
        plotlyRedraw('plotMonteCarlo', traces, layout, { responsive: true });
    }

    // Auto-switch to Max Drawdown for Permutation
    const distMethod = document.getElementById('distMethod');
    const distMetric = document.getElementById('distMetric');
    if (distMethod && distMetric) {
        distMethod.addEventListener('change', () => {
            if (distMethod.value === 'Permutation (Shuffle)') {
                distMetric.value = 'Max Drawdown';
                Array.from(distMetric.options).forEach(opt => {
                    if (opt.value === 'Net Profit') opt.disabled = true;
                });
            } else {
                Array.from(distMetric.options).forEach(opt => {
                    opt.disabled = false;
                });
            }
        });
    }

    // Handle Distribution Generation
    if (btnRunDist) {
        btnRunDist.addEventListener('click', () => {
            const distMethodEl = document.getElementById('distMethod');
            const distSimsEl = document.getElementById('distSims');
            const distPctEl = document.getElementById('distPct');
            const payload = {
                method: distMethodEl ? distMethodEl.value : 'iid',
                num_simulations: distSimsEl ? distSimsEl.value : 1000,
                pct_trades: distPctEl ? distPctEl.value : 100,
                ruin_threshold: 50
            };

            const origBtnText = btnRunDist.innerText;
            btnRunDist.innerText = 'Running...';
            btnRunDist.disabled = true;

            fetch('/api/run_monte_carlo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
                .then(res => res.json())
                .then(data => {
                    btnRunDist.innerText = origBtnText;
                    btnRunDist.disabled = false;
                    if (data.error) {
                        alert(data.error);
                        return;
                    }
                    const distMetricEl = document.getElementById('distMetric');
                    renderDistribution(data, distMetricEl ? distMetricEl.value : 'Max Drawdown');
                })
                .catch(err => {
                    btnRunDist.innerText = origBtnText;
                    btnRunDist.disabled = false;
                    alert('Error running distribution: ' + err);
                });
        });
    }

    function renderDistribution(mc, metricType) {
        if (!mc.sim_returns || !mc.sim_max_dd) return;

        let rawData = metricType === 'Net Profit' ? mc.sim_returns : mc.sim_max_dd;
        const sortedReturns = rawData.slice().sort((a, b) => a - b);
        const pctiles = sortedReturns.map((_, i) => (i / sortedReturns.length) * 100);

        let color = metricType === 'Net Profit' ? '#2563eb' : '#ef4444';
        let ytitle = metricType === 'Net Profit' ? 'Simulated Return (%)' : 'Simulated Max Drawdown (%)';

        const distTrace = {
            x: pctiles, y: sortedReturns, mode: 'lines', name: metricType,
            line: { color: color, width: 3 },
            fill: 'tozeroy',
            fillcolor: metricType === 'Net Profit' ? 'rgba(37,99,235,0.2)' : 'rgba(239,68,68,0.2)',
            hovertemplate: '%{x:.1f}% of Sims<br>%{y:.2f}% ' + metricType + '<extra></extra>'
        };

        const distLayout = {
            title: `Monte Carlo Outcome Distribution: ${metricType}`,
            plot_bgcolor: '#0e1117', paper_bgcolor: '#0e1117',
            font: { color: '#e2e8f0' }, margin: { l: 50, r: 20, t: 40, b: 40 },
            xaxis: { title: 'Percentage of Simulations (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.05)', range: [0, 100] },
            yaxis: { title: ytitle, showgrid: true, gridcolor: 'rgba(255,255,255,0.05)' },
            hovermode: 'x unified',
            hoverlabel: { bgcolor: '#1a1c23', font: { color: '#e2e8f0' }, bordercolor: '#333' }
        };
        document.getElementById('plotDistribution').style.display = 'block';
        plotlyRedraw('plotDistribution', [distTrace], distLayout, { responsive: true });
        window.dispatchEvent(new Event('resize'));
    }

    function renderHeatmap(heatmapData) {
        if (!heatmapData || heatmapData.length === 0) return;
        const container = document.getElementById('heatmapContainer');
        const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

        let html = '<table class="heatmap-table"><thead><tr><th>Year</th>';
        months.forEach(m => html += `<th>${m}</th>`);
        html += '<th style="border-left: 1px solid rgba(255,255,255,0.1);">Total Return</th><th>Max DD</th></tr></thead><tbody>';

        heatmapData.forEach(row => {
            html += `<tr><td>${row.Year}</td>`;
            months.forEach(m => {
                const val = row[m];
                if (val === null || val === undefined) {
                    html += `<td style="background-color: transparent;">-</td>`;
                } else {
                    let color = val > 0 ? `rgba(52, 211, 153, ${Math.min(0.2 + (val / 20), 0.9)})` :
                        val < 0 ? `rgba(251, 113, 133, ${Math.min(0.2 + (Math.abs(val) / 20), 0.9)})` : 'transparent';
                    let mIdx = months.indexOf(m) + 1;
                    html += `<td style="background-color: ${color}; cursor: pointer;" onclick="window.openCalendarModal(${row.Year}, ${mIdx})">${val.toFixed(2)}%</td>`;
                }
            });

            // Total Return
            const tr = row['Total Return'];
            let trColor = tr > 0 ? `rgba(52, 211, 153, ${Math.min(0.3 + (tr / 20), 0.9)})` :
                tr < 0 ? `rgba(251, 113, 133, ${Math.min(0.3 + (Math.abs(tr) / 20), 0.9)})` : 'transparent';
            html += `<td style="background-color: ${trColor}; font-weight: bold; border-left: 1px solid rgba(255,255,255,0.1);">${tr !== undefined ? tr.toFixed(2) + '%' : '-'}</td>`;

            // Max Drawdown
            const dd = row['Max Drawdown'];
            let ddColor = dd > 0 ? `rgba(251, 113, 133, ${Math.min(0.3 + (dd / 20), 0.9)})` : 'transparent';
            html += `<td style="background-color: ${ddColor}; font-weight: bold;">${dd !== undefined ? '-' + dd.toFixed(2) + '%' : '-'}</td>`;

            html += '</tr>';
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    }

    // Ordered, grouped layout for the Strategy Statistics Matrix.
    // label = display text, key = exact backend key from quant_metrics.calculate_stats(),
    // fmt = explicit format type (kept explicit rather than inferred from the key
    // name -- inferring format from substrings in the key is exactly the pattern
    // that caused the stale-key display bugs elsewhere in this app).
    const MATRIX_GROUPS = [
        {
            title: 'Execution & Capacity',
            rows: [
                { label: 'Trades', key: 'Trades', fmt: 'count' },
                { label: 'Active Day Trade Frequency', key: 'Active Day Trade Frequency', fmt: 'num2' },
                { label: 'Daily Trade Frequency', key: 'Daily Trade Frequency', fmt: 'num2' },
                { label: 'Exposure (%)', key: 'Exposure (%)', fmt: 'pct' },
                { label: 'Commission Paid', key: 'Commission Paid', fmt: 'dollar' },
                { label: 'Slippage Paid', key: 'Slippage Paid', fmt: 'dollar' }
            ]
        },
        {
            title: 'Absolute Growth & Returns',
            rows: [
                { label: 'Return (%)', key: 'Return (%)', fmt: 'pct' },
                { label: 'CAGR (%)', key: 'CAGR (%)', fmt: 'pct' }
            ]
        },
        {
            title: 'Execution Precision & Duration',
            rows: [
                { label: 'MAE (Maximum Adverse Excursion) %', key: 'MAE (%)', fmt: 'pct' },
                { label: 'MFE (Maximum Favorable Excursion) %', key: 'MFE (%)', fmt: 'pct' },
                { label: 'Average Winning Trade Duration', key: 'Average Winning Trade Duration', fmt: 'mins' },
                { label: 'Average Losing Trade Duration', key: 'Average Losing Trade Duration', fmt: 'mins' },
                { label: 'Duration Ratio', key: 'Duration Ratio', fmt: 'ratio' }
            ]
        },
        {
            title: 'Trade Dynamics & Win Profile',
            rows: [
                { label: 'Winning (%)', key: 'Winning (%)', fmt: 'pct' },
                { label: 'Percentage of Winning Days', key: 'Percentage of Winning Days', fmt: 'pct' },
                { label: 'Payoff Ratio (Average Win / Average Loss)', key: 'Payoff Ratio', fmt: 'ratio' },
                { label: 'Profit Factor', key: 'Profit Factor', fmt: 'ratio' },
                { label: 'Expectancy ($)', key: 'Expectancy ($)', fmt: 'dollar' },
                { label: 'Average Winning Trade', key: 'Average Winning Trade', fmt: 'dollar' },
                { label: 'Average Losing Trade', key: 'Average Losing Trade', fmt: 'dollar' },
                { label: 'Largest Winning Trade', key: 'Largest Winning Trade', fmt: 'dollar' },
                { label: 'Largest Losing Trade', key: 'Largest Losing Trade', fmt: 'dollar' },
                { label: 'Max Consec. Winners', key: 'Max Consec Winning', fmt: 'int' },
                { label: 'Max Consec. Losers', key: 'Max Consec Lose', fmt: 'int' }
            ]
        },
        {
            title: 'Drawdown & Stress Profile',
            rows: [
                { label: 'Max DD (%)', key: 'Max DD (%)', fmt: 'pct' },
                { label: 'Max Drawdown Duration', key: 'Max Drawdown Duration', fmt: 'days' },
                { label: 'Ulcer Index', key: 'Ulcer Index', fmt: 'pct' },
                { label: 'UPI (Ulcer Performance Index)', key: 'UPI', fmt: 'ratio' },
                { label: 'Bottom 99%', key: 'Top 1%', fmt: 'pct' },
                { label: 'Bottom 95%', key: 'Top 5%', fmt: 'pct' },
                { label: 'Bottom 90%', key: 'Top 10%', fmt: 'pct' }
            ]
        },
        {
            title: 'Statistical Robustness & Risk-Adjusted Metrics',
            rows: [
                { label: 'Annualized Volatility', key: 'Annualized Volatility', fmt: 'pct' },
                { label: 'Sharpe Ratio', key: 'Sharpe', fmt: 'ratio' },
                { label: 'Sortino Ratio', key: 'Sortino', fmt: 'ratio' },
                { label: 'Calmar Ratio (CAGR / Max DD)', key: 'Calmar Ratio', fmt: 'ratio' },
                { label: 'RoMD (Return over Max Drawdown)', key: 'RoMD', fmt: 'ratio' },
                { label: 'K-Ratio (Equity Curve Linearity)', key: 'K-Ratio', fmt: 'ratio' },
                { label: 'Probabilistic Sharpe Ratio (PSR)', key: 'PSR (%)', fmt: 'pct' },
                { label: 'PSR P-Value (vs Zero Alpha)', key: 'P-Value', fmt: 'pvalue_pct' },
                { label: 'Deflated Sharpe Ratio (DSR)', key: 'DSR (%)', fmt: 'pct' },
                { label: 'DSR P-Value (Selection-Adjusted)', key: 'DSR P-Value', fmt: 'pvalue_pct' },
                { label: 'Expected Max Sharpe (SR*)', key: 'Expected Max SR', fmt: 'ratio' },
                { label: 'Trial Sharpe Dispersion (σ_SR)', key: 'Trial Sharpe Std', fmt: 'ratio' },
                { label: 'Trials Tested (K)', key: 'Trials Tested (K)', fmt: 'count' },
                { label: 'Return Skewness (γ₃)', key: 'Skewness', fmt: 'ratio' },
                { label: 'Return Kurtosis (γ₄)', key: 'Kurtosis', fmt: 'ratio' }
            ]
        }
    ];


    function formatMatrixValue(val, fmt) {
        if (val === undefined || val === null || val === 'N/A' || val === '') return 'N/A';
        if (typeof val === 'boolean') return val ? 'True' : 'False';
        const num = parseFloat(val);
        if (isNaN(num)) return String(val);
        if (fmt === 'pvalue_pct') {
            return (num * 100).toFixed(2) + '%';
        }
        switch (fmt) {
            case 'count':
            case 'int':
                return Math.round(num).toLocaleString('en-US');
            case 'pct':
                return num.toFixed(2) + '%';
            case 'mins': {
                const totalMinutes = Math.round(num);
                const h = Math.floor(totalMinutes / 60);
                const m = totalMinutes % 60;
                return h > 0 ? `${h}h ${m}m` : `${m}m`;
            }
            case 'days':
                return Math.round(num).toString() + ' d';
            case 'dollar':
                return (num < 0 ? '-$' : '$') + Math.abs(num).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            case 'ratio':
            case 'num2':
            default:
                return num.toFixed(2);
        }
    }

    function renderMatrix(all, long, short) {
        const container = document.getElementById('matrixContainer');
        if (!container) return;
        if (!all) {
            container.innerHTML = '<p class="empty-text" style="text-align: center; padding: 2.5rem; color: var(--text-secondary);">No backtest metrics available. Please run a backtest first.</p>';
            return;
        }

        let html = `<table class="stats-matrix">
            <thead>
                <tr>
                    <th style="text-align: left; width: 46%;">Metric</th>
                    <th style="text-align: right; width: 18%;">All Trades</th>
                    <th style="text-align: right; width: 18%;">Long Trades</th>
                    <th style="text-align: right; width: 18%;">Short Trades</th>
                </tr>
            </thead>
            <tbody>`;

        MATRIX_GROUPS.forEach(group => {
            html += `<tr class="matrix-group-header"><td colspan="4">${group.title}</td></tr>`;
            group.rows.forEach(row => {
                const vAll = all ? all[row.key] : undefined;
                const vLong = long ? long[row.key] : undefined;
                const vShort = short ? short[row.key] : undefined;
                html += `<tr>
                    <td class="metric-name">${row.label}</td>
                    <td class="metric-value">${formatMatrixValue(vAll, row.fmt)}</td>
                    <td class="metric-value">${formatMatrixValue(vLong, row.fmt)}</td>
                    <td class="metric-value">${formatMatrixValue(vShort, row.fmt)}</td>
                </tr>`;
            });
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    }

    function renderBestWorst(bwData, period) {
        if (!bwData || !bwData[period]) return;

        const labelStr = period.toLowerCase();
        document.getElementById('bwLabelBest').innerText = labelStr;
        document.getElementById('bwLabelWorst').innerText = labelStr;

        const bestContainer = document.getElementById('bwBestContainer');
        const worstContainer = document.getElementById('bwWorstContainer');

        const data = bwData[period];

        let bestHtml = '';
        data.best.forEach(item => {
            bestHtml += `
                <div class="bw-badge">
                    <span class="bw-date">${item.date}</span>
                    <span class="bw-val best">${item.value > 0 ? '+' : ''}${item.value.toFixed(2)}%</span>
                </div>
            `;
        });
        bestContainer.innerHTML = bestHtml;

        let worstHtml = '';
        data.worst.forEach(item => {
            worstHtml += `
                <div class="bw-badge">
                    <span class="bw-date">${item.date}</span>
                    <span class="bw-val worst">${item.value > 0 ? '+' : ''}${item.value.toFixed(2)}%</span>
                </div>
            `;
        });
        worstContainer.innerHTML = worstHtml;
    }

    function renderDrawdownAnalysis(tableData, curveData) {
        const tbody = document.querySelector('#drawdownTable tbody');
        if (!tbody) return;

        let html = '';
        tableData.forEach((row, idx) => {
            html += `
                <tr>
                    <td style="color: var(--danger); font-weight: 500;">${row.depth.toFixed(2)}%</td>
                    <td>${row.days}</td>
                    <td>${row.start_date}</td>
                    <td>${row.end_date}</td>
                </tr>
            `;
        });
        tbody.innerHTML = html;

        if (!curveData || !curveData.x || curveData.x.length === 0) return;

        const trace = {
            x: curveData.x,
            y: curveData.y,
            fill: 'tozeroy',
            type: 'scatter',
            mode: 'lines',
            line: { color: 'rgba(239, 68, 68, 0.9)', width: 1.5 },
            fillcolor: 'rgba(239, 68, 68, 0.4)',
            name: 'Drawdown',
            hovertemplate: '<b>Date:</b> %{x}<br><b>Drawdown:</b> %{y:.2f}%<extra></extra>',
        };

        const layout = {
            plot_bgcolor: '#0e1117', paper_bgcolor: '#0e1117',
            font: { color: '#fafafa' }, margin: { l: 50, r: 20, t: 20, b: 80 },
            xaxis: { showgrid: true, gridcolor: 'rgba(255,255,255,0.03)', zerolinecolor: 'rgba(255,255,255,0.03)', tickangle: 45 },
            yaxis: { title: 'Drawdowns (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.03)', zerolinecolor: 'rgba(255,255,255,0.03)', range: [Math.min(...curveData.y) * 1.1, 0] },
            hovermode: 'x',
            hoverlabel: { bgcolor: '#1a1c23', font: { color: '#e2e8f0' }, bordercolor: '#333' }
        };

        plotlyRedraw('plotDrawdownAnalysis', [trace], layout, { responsive: true });
    }

    const safeNum = (v, d = 2) => (v !== null && v !== undefined && !isNaN(Number(v))) ? Number(v).toFixed(d) : '0.00';

    function renderTradingTimeDist(data) {
        const boxContainer = document.getElementById('sessionBoxes');
        if (!data || !data.sessions) {
            if (boxContainer) boxContainer.innerHTML = '<p class="empty-text" style="text-align: center; padding: 1.5rem; color: var(--text-secondary);">No trading time distribution data available. Please run a backtest first.</p>';
            return;
        }

        // Colors mapping
        const colors = {
            'Asian': '#a855f7', // Purple
            'European': '#d946ef', // Pink/Magenta
            'American': '#ea580c' // Orange
        };

        // 1. Donut Chart
        const labels = ['Asian', 'European', 'American'];
        const values = labels.map(l => (data.sessions[l] && data.sessions[l].pct_of_total !== undefined) ? Number(data.sessions[l].pct_of_total) : 0);
        const pieColors = labels.map(l => colors[l]);

        const donutTrace = {
            labels: labels,
            values: values,
            type: 'pie',
            hole: 0.65,
            marker: { colors: pieColors },
            textinfo: 'none',
            hoverinfo: 'label+percent',
            showlegend: false
        };

        const donutLayout = {
            plot_bgcolor: 'rgba(0,0,0,0)', paper_bgcolor: 'rgba(0,0,0,0)',
            autosize: true,
            margin: { l: 20, r: 20, t: 20, b: 20 },
            annotations: [{
                font: { size: 14, color: '#94a3b8' },
                showarrow: false,
                text: 'Sessions',
                x: 0.5, y: 0.5
            }]
        };
        plotlyRedraw('plotSessionDonut', [donutTrace], donutLayout, { responsive: true });

        // 2. Session Boxes
        if (boxContainer) {
            boxContainer.innerHTML = '';
            labels.forEach(session => {
                const sData = data.sessions[session] || { count: 0, return: 0 };
                const retVal = Number(sData.return || 0);
                const retColor = retVal >= 0 ? 'var(--success)' : 'var(--danger)';
                boxContainer.innerHTML += `
                    <div>
                        <h5 style="margin: 0 0 5px 0; color: var(--text-secondary); font-size: 0.8rem;">${session} session</h5>
                        <div style="display: flex; gap: 10px;">
                            <div style="flex: 1; border: 1px solid ${colors[session]}; background-color: rgba(255,255,255,0.02); border-radius: 4px; padding: 10px;">
                                <p style="margin: 0; font-size: 0.8rem; color: var(--text-secondary);">Num. of trades</p>
                                <p style="margin: 5px 0 0 0; font-size: 1.2rem; font-weight: 600;">${(sData.count || 0).toLocaleString()}</p>
                            </div>
                            <div style="flex: 1; border: 1px solid ${colors[session]}; background-color: rgba(255,255,255,0.02); border-radius: 4px; padding: 10px;">
                                <p style="margin: 0; font-size: 0.8rem; color: var(--text-secondary);">Approx. return</p>
                                <p style="margin: 5px 0 0 0; font-size: 1.2rem; font-weight: 600; color: ${retColor};">${safeNum(retVal, 2)}%</p>
                            </div>
                        </div>
                    </div>
                `;
            });
        }

        window.currentTradingTimeData = data;
        window.updateTimeDistChart('volume', document.getElementById('btnDistVolume'));

        if (data.scatter_data && data.scatter_data.length > 0) {
            renderTradeDurationChart(data.scatter_data);
            window.updateScatterTimeChart();
        }
    }

    function formatDuration(sec) {
        if (sec < 60) return `${Math.floor(sec)}s`;
        const totalMins = Math.floor(sec / 60);
        const h = Math.floor(totalMins / 60);
        const m = totalMins % 60;
        return h > 0 ? `${h}h ${m}m` : `${m}m`;
    }

    function renderTradeDurationChart(scatterData) {
        if (!document.getElementById('plotTradeDurationPerformance')) return;

        const winning = scatterData.filter(d => d.pnl > 0);
        const losing = scatterData.filter(d => d.pnl <= 0);

        const traceWin = {
            x: winning.map(d => d.duration_sec),
            y: winning.map(d => d.pnl),
            mode: 'markers',
            type: 'scatter',
            marker: { color: '#089981', size: 5 },
            name: 'Win',
            text: winning.map(d => `Duration: ${formatDuration(d.duration_sec)}<br>PnL: $${Number(d.pnl || 0).toFixed(2)}`),
            hoverinfo: 'text'
        };
        const traceLoss = {
            x: losing.map(d => d.duration_sec),
            y: losing.map(d => d.pnl),
            mode: 'markers',
            type: 'scatter',
            marker: { color: '#F23645', size: 5 },
            name: 'Loss',
            text: losing.map(d => `Duration: ${formatDuration(d.duration_sec)}<br>PnL: $${Number(d.pnl || 0).toFixed(2)}`),
            hoverinfo: 'text'
        };

        const maxSec = scatterData.length > 0 ? Math.max(10, ...scatterData.map(d => d.duration_sec || 0)) : 10;
        const tickvals = [];
        const ticktext = [];
        const step = Math.max(1, Math.ceil(maxSec / 10));
        for (let i = 0; i <= maxSec; i += step) {
            tickvals.push(i);
            ticktext.push(formatDuration(i));
        }

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            autosize: true,
            margin: { t: 10, r: 10, l: 50, b: 60 },
            showlegend: false,
            xaxis: {
                tickvals: tickvals,
                ticktext: ticktext,
                gridcolor: 'rgba(255,255,255,0.05)',
                tickangle: -45,
                tickfont: { color: '#94a3b8' }
            },
            yaxis: {
                gridcolor: 'rgba(255,255,255,0.05)',
                tickprefix: '$',
                tickfont: { color: '#94a3b8' },
                zerolinecolor: 'rgba(255,255,255,0.1)'
            }
        };
        plotlyRedraw('plotTradeDurationPerformance', [traceWin, traceLoss], layout, { responsive: true });
    }

    window.updateScatterTimeChart = function () {
        const scatterData = window.currentTradingTimeData ? window.currentTradingTimeData.scatter_data : null;
        if (!scatterData || !document.getElementById('plotTradeTimePerformance')) return;

        const sel = document.getElementById('tradeTimeSelect');
        const mode = sel ? sel.value : 'entry';

        const winning = scatterData.filter(d => d.pnl > 0);
        const losing = scatterData.filter(d => d.pnl <= 0);

        const getX = d => mode === 'entry' ? d.entry_time_str : d.exit_time_str;

        const traceWin = {
            x: winning.map(getX),
            y: winning.map(d => d.pnl),
            mode: 'markers',
            type: 'scatter',
            marker: { color: '#089981', size: 5 },
            name: 'Win',
            text: winning.map(d => `Time: ${(getX(d) || '').split(' ')[1] || ''}<br>PnL: $${Number(d.pnl || 0).toFixed(2)}`),
            hoverinfo: 'text'
        };
        const traceLoss = {
            x: losing.map(getX),
            y: losing.map(d => d.pnl),
            mode: 'markers',
            type: 'scatter',
            marker: { color: '#F23645', size: 5 },
            name: 'Loss',
            text: losing.map(d => `Time: ${(getX(d) || '').split(' ')[1] || ''}<br>PnL: $${Number(d.pnl || 0).toFixed(2)}`),
            hoverinfo: 'text'
        };

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            autosize: true,
            margin: { t: 10, r: 10, l: 50, b: 60 },
            showlegend: false,
            xaxis: {
                type: 'date',
                tickformat: '%H:%M',
                gridcolor: 'rgba(255,255,255,0.05)',
                tickangle: -45,
                tickfont: { color: '#94a3b8' }
            },
            yaxis: {
                gridcolor: 'rgba(255,255,255,0.05)',
                tickprefix: '$',
                tickfont: { color: '#94a3b8' },
                zerolinecolor: 'rgba(255,255,255,0.1)'
            }
        };
        plotlyRedraw('plotTradeTimePerformance', [traceWin, traceLoss], layout, { responsive: true });
    };

    window.updateTimeDistChart = function (type, element) {
        const data = window.currentTradingTimeData;
        if (!data || !data.buckets) return;

        if (element) {
            document.querySelectorAll('.time-seg-btn').forEach(btn => {
                btn.style.background = 'transparent';
                btn.style.color = 'var(--text-secondary)';
                btn.classList.remove('active-seg');
            });
            element.style.background = 'rgba(185, 28, 28, 0.8)';
            element.style.color = 'white';
            element.classList.add('active-seg');
        }

        const formatBucket = (b) => {
            const h = Math.floor(b);
            const m = (b % 1 === 0.5) ? '30' : '00';
            return `${h.toString().padStart(2, '0')}:${m}`;
        };
        const timeLabels = data.buckets.map(formatBucket);

        const colors = {
            'Asian': '#a855f7', // Purple
            'European': '#d946ef', // Pink/Magenta
            'American': '#ea580c' // Orange
        };

        const barColors = data.buckets.map(h => {
            if (h >= 3 && h < 8) return colors['European'];
            if (h >= 8 && h < 17) return colors['American'];
            return colors['Asian'];
        });

        let yData = [];
        let hoverTemp = '';

        const customData = data.buckets.map((b, i) => {
            return [
                data.pf_total && data.pf_total[i] !== undefined ? safeNum(data.pf_total[i], 2) : '0.00',
                data.pf_long && data.pf_long[i] !== undefined ? safeNum(data.pf_long[i], 2) : '0.00',
                data.pf_short && data.pf_short[i] !== undefined ? safeNum(data.pf_short[i], 2) : '0.00',
                data.volume_by_entry && data.volume_by_entry[i] !== undefined ? (data.volume_by_entry[i] || 0) : 0
            ];
        });

        if (type === 'volume') {
            yData = data.volume_by_entry;
            hoverTemp = 'Time: %{x}<br>Trades: %{y}<br>PF: %{customdata[0]}<br>PF (L): %{customdata[1]}<br>PF (S): %{customdata[2]}<extra></extra>';
        } else if (type === 'pnl_entry') {
            yData = data.pnl_by_entry;
            hoverTemp = 'Time: %{x}<br>PnL: %{y}%<br>PF: %{customdata[0]}<br>PF (L): %{customdata[1]}<br>PF (S): %{customdata[2]}<extra></extra>';
        } else if (type === 'pnl_exit') {
            yData = data.pnl_by_exit;
            hoverTemp = 'Time: %{x}<br>PnL: %{y}%<extra></extra>';
        } else if (type === 'pf_total') {
            yData = data.pf_total;
            hoverTemp = 'Time: %{x}<br>PF: %{y}<br>Trades: %{customdata[3]}<extra></extra>';
        } else if (type === 'pf_long') {
            yData = data.pf_long;
            hoverTemp = 'Time: %{x}<br>PF (L): %{y}<br>Trades: %{customdata[3]}<extra></extra>';
        } else if (type === 'pf_short') {
            yData = data.pf_short;
            hoverTemp = 'Time: %{x}<br>PF (S): %{y}<br>Trades: %{customdata[3]}<extra></extra>';
        }

        const barTrace = {
            x: timeLabels,
            y: yData,
            type: 'bar',
            marker: { color: barColors },
            customdata: customData,
            hovertemplate: hoverTemp
        };

        const barLayout = {
            plot_bgcolor: 'rgba(0,0,0,0)', paper_bgcolor: 'rgba(0,0,0,0)',
            autosize: true,
            margin: { l: 40, r: 20, t: 10, b: 50 },
            xaxis: { showgrid: false, zeroline: false, tickangle: -45, tickfont: { color: '#94a3b8', size: 10 } },
            yaxis: { showgrid: true, gridcolor: 'rgba(255,255,255,0.05)', showticklabels: true, tickfont: { color: '#94a3b8' } }
        };

        plotlyRedraw('plotTimeDistribution', [barTrace], barLayout, { responsive: true });
    };

    function renderDayOfWeekDist(data) {
        if (!data) return;

        const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
        const colors = ['#ea580c', '#6366f1', '#a855f7', '#be123c', '#0284c7'];

        // 1. Line Charts
        const traces = [];
        days.forEach((day, index) => {
            const dData = data[day];
            if (dData && dData.curve_x && dData.curve_y && dData.curve_x.length > 0) {
                traces.push({
                    x: dData.curve_x,
                    y: dData.curve_y,
                    name: day,
                    mode: 'lines',
                    line: { color: colors[index], width: 1.5 },
                    hovertemplate: `<b>${day}</b><br>Trade %{x}<br>Return: %{y}%<extra></extra>`
                });
            }
        });

        const layout = {
            plot_bgcolor: 'rgba(0,0,0,0)', paper_bgcolor: 'rgba(0,0,0,0)',
            autosize: true,
            font: { color: '#94a3b8' }, margin: { l: 50, r: 20, t: 20, b: 40 },
            xaxis: { showgrid: true, gridcolor: 'rgba(255,255,255,0.05)', showticklabels: false },
            yaxis: { title: 'Return (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.05)' },
            hovermode: 'x unified',
            hoverlabel: { bgcolor: '#1a1c23', font: { color: '#e2e8f0' }, bordercolor: '#333' },
            legend: { orientation: 'h', y: 1.1, x: 0.5, xanchor: 'center' }
        };

        plotlyRedraw('plotDayOfWeekCurves', traces, layout, { responsive: true });

        // 2. Summary Boxes
        const boxContainer = document.getElementById('dowBoxes');
        boxContainer.innerHTML = '';
        days.forEach((day, index) => {
            const dData = data[day];
            if (dData) {
                const pfTotal = safeNum(dData.pf_total, 2);
                const pfLong = safeNum(dData.pf_long, 2);
                const pfShort = safeNum(dData.pf_short, 2);
                boxContainer.innerHTML += `
                    <div style="background-color: ${colors[index]}; opacity: 0.9; padding: 10px; border-radius: 4px; text-align: center; color: white; display: flex; flex-direction: column; justify-content: center; min-height: 80px;">
                        <p style="margin: 0 0 2px 0; font-size: 1.2rem; font-weight: bold;">${dData.trading_days}</p>
                        <p style="margin: 0 0 8px 0; font-size: 0.85rem; font-weight: 500;">${day}</p>
                        <div style="font-size: 0.75rem; background: rgba(0,0,0,0.2); padding: 5px; border-radius: 4px; display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 2px;">
                            <div><span style="opacity:0.8; font-size: 0.65rem;">P.F.</span><br><b>${pfTotal}</b></div>
                            <div><span style="opacity:0.8; font-size: 0.65rem;">P.F.(L)</span><br><b>${pfLong}</b></div>
                            <div><span style="opacity:0.8; font-size: 0.65rem;">P.F.(S)</span><br><b>${pfShort}</b></div>
                        </div>
                    </div>
                `;
            }
        });
    }

    // --- Walk-Forward Optimization (WFO) Logic ---
    function attachWFOListeners() {
        const table = document.getElementById('wfoInputTable');
        if (!table) return;

        function updateWFOStepCounts() {
            let totalCombos = 1;
            let anyChecked = false;

            table.querySelectorAll('tbody tr').forEach(row => {
                const isChecked = row.querySelector('.wfo-check').checked;
                if (!isChecked) {
                    row.querySelector('.wfo-step-count').innerText = "1";
                    return;
                }
                anyChecked = true;

                const start = parseFloat(row.querySelector('.wfo-start').value) || 0;
                const end = parseFloat(row.querySelector('.wfo-end').value) || 0;
                const step = parseFloat(row.querySelector('.wfo-step').value) || 0;

                let count = 1;
                if (step > 0 && end >= start) {
                    count = Math.round((end - start) / step) + 1;
                }

                row.querySelector('.wfo-step-count').innerText = count;
                totalCombos *= count;
            });

            if (!anyChecked) totalCombos = 0;
            const el = document.getElementById('wfoTotalCombos');
            if (el) el.innerText = totalCombos.toLocaleString();
        }

        table.addEventListener('input', updateWFOStepCounts);
        table.addEventListener('change', updateWFOStepCounts);

        const selectAll = document.getElementById('wfoSelectAll');
        if (selectAll) {
            selectAll.addEventListener('change', (e) => {
                const checked = e.target.checked;
                table.querySelectorAll('.wfo-check').forEach(cb => cb.checked = checked);
                updateWFOStepCounts();
            });
        }
        updateWFOStepCounts();
    }

    const btnRunWalkForward = document.getElementById('btnRunWalkForward');
    if (btnRunWalkForward) {
        btnRunWalkForward.addEventListener('click', () => {
            const script = currentScript;
            const data = currentData;
            if (!script || !data) return alert("Select strategy script and data first from the sidebar.");

            const totalCombosStr = (document.getElementById('wfoTotalCombos')?.innerText || '0').replace(/,/g, '');
            const totalCombos = parseInt(totalCombosStr) || 0;
            if (totalCombos === 0) return alert("Select at least one parameter to optimize during Walk-Forward training.");
            if (totalCombos > 2000) return alert(`Total combinations per window (${totalCombos}) exceeds the limit of 2,000. Please reduce ranges or increase step size.`);

            const optimizable_params = [];
            document.querySelectorAll('#wfoInputTable tbody tr').forEach(row => {
                if (row.querySelector('.wfo-check').checked) {
                    optimizable_params.push({
                        name: row.getAttribute('data-param'),
                        start: parseFloat(row.querySelector('.wfo-start').value),
                        end: parseFloat(row.querySelector('.wfo-end').value),
                        step: parseFloat(row.querySelector('.wfo-step').value)
                    });
                }
            });

            const wfoMode = document.getElementById('wfoMode')?.value || 'Rolling';
            const trainQuarters = parseInt(document.getElementById('wfoTrainQuarters')?.value) || 4;
            const testQuarters = parseInt(document.getElementById('wfoTestQuarters')?.value) || 2;
            const stepQuarters = parseInt(document.getElementById('wfoStepQuarters')?.value) || 2;
            const objectiveMetric = document.getElementById('wfoObjective')?.value || 'sharpe';

            const origBtnText = btnRunWalkForward.innerText;
            btnRunWalkForward.disabled = true;
            btnRunWalkForward.innerText = 'Running WFO...';

            const statusBanner = document.getElementById('wfoStatusBanner');
            const statusText = document.getElementById('wfoStatusText');
            const statusProgress = document.getElementById('wfoStatusProgress');
            const resultsContainer = document.getElementById('wfoResultsContainer');
            const cycleTableBody = document.querySelector('#wfoCycleTable tbody');

            if (statusBanner) statusBanner.style.display = 'block';
            if (statusText) statusText.innerText = 'Initializing Walk-Forward Optimization...';
            if (statusProgress) statusProgress.innerText = '0/0';
            if (resultsContainer) resultsContainer.style.display = 'block';
            if (cycleTableBody) cycleTableBody.innerHTML = '';

            const payload = {
                script,
                data,
                base_params: getParamsFromUI(),
                optimizable_params,
                wfo_mode: wfoMode,
                train_quarters: trainQuarters,
                test_quarters: testQuarters,
                step_quarters: stepQuarters,
                objective_metric: objectiveMetric,
                start_quarter: selectedStartQuarter,
                end_quarter: selectedEndQuarter
            };

            fetch('/api/walk_forward', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(response => {
                if (!response.ok) {
                    return response.json().then(errData => {
                        throw new Error(errData.error || 'Server error running Walk-Forward');
                    });
                }
                const reader = response.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let buffer = '';

                function readChunk() {
                    return reader.read().then(({ done, value }) => {
                        if (done) {
                            btnRunWalkForward.disabled = false;
                            btnRunWalkForward.innerText = origBtnText;
                            if (statusBanner) statusBanner.style.display = 'none';
                            return;
                        }

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop(); // Keep uncompleted line in buffer

                        lines.forEach(line => {
                            if (!line.trim()) return;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.error) {
                                    alert("WFO Error: " + msg.error);
                                    if (statusBanner) statusBanner.style.display = 'none';
                                    btnRunWalkForward.disabled = false;
                                    btnRunWalkForward.innerText = origBtnText;
                                    return;
                                }

                                if (msg.type === 'cycle_update') {
                                    if (statusText) statusText.innerText = `Completed Cycle ${msg.cycle}/${msg.total_cycles}: Train [${msg.train_window}] -> Test [${msg.test_window}]`;
                                    if (statusProgress) statusProgress.innerText = `${msg.cycle}/${msg.total_cycles}`;

                                    const isSharpe = parseFloat(msg.is_metrics?.Sharpe ?? 0).toFixed(2);
                                    const isPnL = parseFloat(msg.is_metrics?.['Net Profit'] ?? msg.is_metrics?.Total_PnL ?? 0);
                                    const oosSharpe = parseFloat(msg.oos_metrics?.Sharpe ?? 0).toFixed(2);
                                    const oosPnL = parseFloat(msg.oos_metrics?.['Net Profit'] ?? msg.oos_metrics?.Total_PnL ?? 0);
                                    const oosWinRate = parseFloat(msg.oos_metrics?.['Winning (%)'] ?? msg.oos_metrics?.Win_Rate ?? 0).toFixed(1);
                                    const wfeCagr = msg.window_wfe_cagr !== undefined ? `${msg.window_wfe_cagr}%` : '-';
                                    const isProfitable = oosPnL > 0;

                                    const paramsStr = Object.entries(msg.frozen_params || {})
                                        .map(([k, v]) => `${k}: ${v}`).join(', ');

                                    const rowHtml = `
                                        <tr>
                                            <td><strong>${msg.cycle}</strong></td>
                                            <td>${msg.train_window}</td>
                                            <td>${msg.test_window}</td>
                                            <td><code style="background:rgba(255,255,255,0.06); padding:2px 6px; border-radius:4px; font-size:11px;">${paramsStr}</code></td>
                                            <td style="color:${isSharpe >= 0 ? 'var(--success)' : 'var(--danger)'}">${isSharpe}</td>
                                            <td style="color:${isPnL >= 0 ? 'var(--success)' : 'var(--danger)'}">$${isPnL.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                                            <td style="color:${oosSharpe >= 0 ? 'var(--success)' : 'var(--danger)'}; font-weight:bold;">${oosSharpe}</td>
                                            <td style="color:${oosPnL >= 0 ? 'var(--success)' : 'var(--danger)'}; font-weight:bold;">$${oosPnL.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                                            <td>${oosWinRate}%</td>
                                            <td>${wfeCagr}</td>
                                            <td><span style="padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; background:${isProfitable ? 'rgba(34, 197, 94, 0.2)' : 'rgba(239, 68, 68, 0.2)'}; color:${isProfitable ? 'var(--success)' : 'var(--danger)'};">${isProfitable ? 'PROFIT' : 'LOSS'}</span></td>
                                        </tr>
                                    `;
                                    if (cycleTableBody) cycleTableBody.innerHTML += rowHtml;
                                } else if (msg.type === 'final_summary') {
                                    const oosM = msg.overall_oos_metrics || {};
                                    const netProfit = parseFloat(oosM['Net Profit'] ?? oosM.Total_PnL ?? 0);
                                    const cagr = parseFloat(oosM['CAGR (%)'] ?? oosM.CAGR ?? 0).toFixed(2);
                                    const sharpe = parseFloat(oosM.Sharpe ?? 0).toFixed(2);
                                    const maxDD = parseFloat(oosM['Max Drawdown'] ?? oosM['Max DD (%)'] ?? 0).toFixed(2);

                                    const pnlEl = document.getElementById('wfoOOSPnL');
                                    if (pnlEl) {
                                        pnlEl.innerText = (netProfit >= 0 ? '+$' : '-$') + Math.abs(netProfit).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                                        pnlEl.style.color = netProfit >= 0 ? 'var(--success)' : 'var(--danger)';
                                    }
                                    const cagrEl = document.getElementById('wfoOOSCagr');
                                    if (cagrEl) cagrEl.innerText = `CAGR: ${cagr}%`;

                                    const sharpeEl = document.getElementById('wfoOOSSharpe');
                                    if (sharpeEl) {
                                        sharpeEl.innerText = `Sharpe: ${sharpe}`;
                                        sharpeEl.style.color = sharpe >= 0 ? 'var(--text-primary)' : 'var(--danger)';
                                    }
                                    const maxDDEl = document.getElementById('wfoOOSMaxDD');
                                    if (maxDDEl) maxDDEl.innerText = `Max DD: ${maxDD}%`;

                                    const wfeValEl = document.getElementById('wfoWFEVal');
                                    if (wfeValEl) wfeValEl.innerText = `${msg.wfe_cagr}%`;
                                    const wfeSharpeEl = document.getElementById('wfoWFESharpe');
                                    if (wfeSharpeEl) wfeSharpeEl.innerText = `Sharpe WFE: ${msg.wfe_sharpe}%`;

                                    const stabEl = document.getElementById('wfoStability');
                                    if (stabEl) stabEl.innerText = `${msg.param_stability_pct}%`;
                                    const winWinEl = document.getElementById('wfoProfitableWindows');
                                    if (winWinEl) winWinEl.innerText = `Profitable Windows: ${msg.pct_profitable_windows}%`;

                                    // Render Stitched OOS Cumulative Equity Curve
                                    if (msg.stitched_equity_curve && msg.stitched_equity_curve.pct && msg.stitched_equity_curve.pct.length > 0) {
                                        plotEquity('plotWFOEquityMain', msg.stitched_equity_curve, '#22c55e', 'Stitched OOS');
                                    }

                                    if (statusBanner) statusBanner.style.display = 'none';
                                    btnRunWalkForward.disabled = false;
                                    btnRunWalkForward.innerText = origBtnText;
                                }
                            } catch (e) {
                                console.error("Error parsing WFO chunk:", e, line);
                            }
                        });

                        return readChunk();
                    });
                }

                return readChunk();
            }).catch(err => {
                alert("Walk-Forward Error: " + err.message);
                if (statusBanner) statusBanner.style.display = 'none';
                btnRunWalkForward.disabled = false;
                btnRunWalkForward.innerText = origBtnText;
            });
        });
    }

    // --- Permutation Test ---
    const btnRunPermutation = document.getElementById('btnRunPermutation');
    if (btnRunPermutation) {
        btnRunPermutation.addEventListener('click', () => {
            if (!currentScript || !currentData) return alert("Select script and data first.");
            const runs = document.getElementById('permRuns').value || 1000;

            const origBtnText = btnRunPermutation.innerText;
            btnRunPermutation.innerText = 'Running...';
            btnRunPermutation.disabled = true;

            fetch('/api/permutation_test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    script: currentScript,
                    data: currentData,
                    params: getParamsFromUI(),
                    n_sims: parseInt(runs)
                })
            }).then(r => r.json()).then(res => {
                btnRunPermutation.innerText = origBtnText;
                btnRunPermutation.disabled = false;
                if (res.error) return alert(res.error);

                document.getElementById('permutationResults').style.display = 'block';
                document.getElementById('permOrigPnL').innerText = (res.original_pnl < 0 ? '-$' : '$') + Math.abs(res.original_pnl).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                document.getElementById('permMedianPnL').innerText = (res.median_pnl < 0 ? '-$' : '$') + Math.abs(res.median_pnl).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                document.getElementById('permProfitable').innerText = `${res.pct_profitable.toFixed(1)}%`;
                document.getElementById('permBeating').innerText = `${res.pct_beating_original.toFixed(1)}%`;
                document.getElementById('permPValue').innerText = res.p_value.toFixed(4);

                const verdictEl = document.getElementById('permVerdict');
                if (res.p_value < 0.05) {
                    verdictEl.innerText = 'PASS: Edge is statistically significant (p < 0.05)';
                    verdictEl.style.color = 'var(--success)';
                } else if (res.p_value < 0.10) {
                    verdictEl.innerText = 'MARGINAL: Edge is borderline significant (0.05 < p < 0.10)';
                    verdictEl.style.color = 'orange';
                } else {
                    verdictEl.innerText = 'FAIL: Edge is NOT statistically significant (p >= 0.10)';
                    verdictEl.style.color = 'var(--danger)';
                }
            }).catch(e => {
                btnRunPermutation.innerText = origBtnText;
                btnRunPermutation.disabled = false;
                alert(e);
            });
        });
    }

    // --- Multi-Market Comparison ---
    let secondaryDataPaths = [];
    const btnSelectSecondary = document.getElementById('btnSelectSecondary');
    if (btnSelectSecondary) {
        btnSelectSecondary.addEventListener('click', () => {
            fetch('/api/select_data_multiple').then(r => r.json()).then(data => {
                if (data.paths && data.paths.length > 0) {
                    data.paths.forEach(p => {
                        if (!secondaryDataPaths.includes(p)) {
                            secondaryDataPaths.push(p);
                        }
                    });
                    document.getElementById('multiMarketSelectedFiles').innerText = `${secondaryDataPaths.length} additional market(s) selected:\n` + secondaryDataPaths.join('\n');
                }
            });
        });
    }

    const btnRunMultiMarket = document.getElementById('btnRunMultiMarket');
    if (btnRunMultiMarket) {
        btnRunMultiMarket.addEventListener('click', () => {
            if (!currentScript || !currentData) return alert("Select primary script and data first from sidebar.");
            if (secondaryDataPaths.length === 0) return alert("Please select at least one additional market to compare.");

            let currentDataArr = Array.isArray(currentData) ? currentData : [currentData];
            const allPaths = currentDataArr.concat(secondaryDataPaths);

            const origBtnText = btnRunMultiMarket.innerText;
            btnRunMultiMarket.innerText = 'Running...';
            btnRunMultiMarket.disabled = true;

            fetch('/api/multi_market', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    script: currentScript,
                    data_paths: allPaths,
                    params: getParamsFromUI()
                })
            }).then(r => r.json()).then(res => {
                btnRunMultiMarket.innerText = origBtnText;
                btnRunMultiMarket.disabled = false;
                if (res.error) return alert(res.error);

                document.getElementById('multiMarketResults').style.display = 'block';

                // Render Core Table Headers
                const headersRow = document.getElementById('mmCoreHeaders');
                headersRow.innerHTML = '<th>Metric</th>';
                res.forEach(m => {
                    headersRow.innerHTML += `<th>${m.market}</th>`;
                });

                // Render Core Table Body
                const tbody = document.querySelector('#mmCoreTable tbody');
                tbody.innerHTML = '';

                const formatMoney = (val) => {
                    if (typeof val === 'number') {
                        return (val >= 0 ? '$' : '-$') + Math.abs(val).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
                    }
                    return val;
                };

                const metricsList = [
                    { label: "Total Trades", key: "Trades" },
                    { label: "Return (%)", key: "Return (%)" },
                    { label: "Max DD (%)", key: "Max DD (%)" },
                    { label: "CAGR (%)", key: "CAGR (%)" },
                    { label: "Calmar Ratio", key: "Calmar Ratio" },
                    { label: "Profit Factor", key: "Profit Factor" },
                    { label: "Sharpe", key: "Sharpe" },
                    { label: "Sortino", key: "Sortino" },
                    { label: "P-Value", key: "P-Value" }
                ];

                metricsList.forEach(({ label, key: metric }) => {
                    let rowHtml = `<tr><td><strong>${label}</strong></td>`;
                    res.forEach(m => {
                        let val = m.metrics[metric];
                        let color = 'inherit';
                        if (metric === 'P-Value') {
                            const num = parseFloat(val);
                            if (num <= 0.05) color = 'var(--success)';
                            else color = 'var(--danger)';
                        }
                        rowHtml += `<td style="color: ${color}">${val}</td>`;
                    });
                    rowHtml += `</tr>`;
                    tbody.innerHTML += rowHtml;
                });

                // Render DD Correlation
                const corrEl = document.getElementById('ddCorrContainer');
                if (res[0].dd_corr !== undefined) {
                    corrEl.innerText = `Drawdown Correlation (Market 1 & 2): ${res[0].dd_corr}`;
                } else {
                    corrEl.innerText = '';
                }

                // Render Yearly Breakdowns
                const yearlyContainer = document.getElementById('mmYearlyContainer');
                yearlyContainer.innerHTML = '';
                // Only for secondary markets (index > 0)
                res.slice(1).forEach(m => {
                    let html = `<h4 style="color: var(--text-primary); margin-bottom: 10px; margin-top: 20px;">${m.market} Year-by-Year</h4>`;
                    html += `<div class="table-container"><table class="mm-yearly-table" style="width: 100%;"><thead><tr><th>Year</th><th>Trades</th><th>Return</th><th>Max DD</th><th>PF</th><th>PF (L)</th><th>PF (S)</th></tr></thead><tbody>`;
                    m.yearly.forEach(y => {
                        html += `<tr>
                            <td>${y.Year}</td>
                            <td>${y.Trades}</td>
                            <td>${formatMoney(y['Return (%)'])}</td>
                            <td>${formatMoney(y['Max DD (%)'])}</td>
                            <td>${y.PF}</td>
                            <td>${y['PF (L)']}</td>
                            <td>${y['PF (S)']}</td>
                        </tr>`;
                    });
                    html += `</tbody></table></div>`;
                    yearlyContainer.innerHTML += html;
                });

            }).catch(e => {
                btnRunMultiMarket.innerText = origBtnText;
                btnRunMultiMarket.disabled = false;
                alert(e);
            });
        });
    }
    // --- Calendar Trade Viewer Logic ---
    let priceChart, volumeChart, candleSeries, volumeSeries;
    let dayChartSyncing = false;
    let activeDayRowsByTime = new Map();

    window.openCalendarModal = function (year, monthIdx) {
        window.currentCalYear = year;
        window.currentCalMonth = monthIdx;

        if (!currentData || currentData.length === 0) {
            alert('No data path selected. Please run backtest first.');
            return;
        }

        loadingOverlay.style.display = 'flex';
        loadingOverlay.querySelector('p').innerText = 'Fetching month data...';

        fetch(`/api/month_calendar?year=${year}&month=${monthIdx}`)
            .then(res => res.json())
            .then(data => {
                loadingOverlay.style.display = 'none';
                if (data.error) {
                    alert(data.error);
                    return;
                }

                const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
                document.getElementById('calMonthTitle').innerText = `${monthNames[monthIdx - 1]} ${year}`;

                document.getElementById('calStatReturn').innerText = data['Return (%)'].toFixed(2) + '%';
                document.getElementById('calStatReturn').style.color = data['Return (%)'] > 0 ? 'var(--success)' : (data['Return (%)'] < 0 ? 'var(--danger)' : 'var(--text-primary)');

                document.getElementById('calStatDD').innerText = data['Drawdown (%)'].toFixed(2) + '%';
                document.getElementById('calStatDD').style.color = data['Drawdown (%)'] > 0 ? 'var(--danger)' : 'var(--text-primary)';

                document.getElementById('calStatPF').innerText = data['PF'].toFixed(2);
                document.getElementById('calStatPFL').innerText = data['PF (L)'].toFixed(2);
                document.getElementById('calStatPFS').innerText = data['PF (S)'].toFixed(2);

                const grid = document.getElementById('calendarGrid');
                grid.innerHTML = '';

                const firstDay = new Date(year, monthIdx - 1, 1).getDay();
                const daysInMonth = new Date(year, monthIdx, 0).getDate();

                let currentWeekPnl = 0;
                let currentWeekTrades = 0;

                for (let i = 0; i < firstDay; i++) {
                    grid.innerHTML += `<div class="cal-day empty"></div>`;
                }

                window.validTradingDays = [];

                for (let i = 1; i <= daysInMonth; i++) {
                    const dateStr = `${year}-${String(monthIdx).padStart(2, '0')}-${String(i).padStart(2, '0')}`;
                    const dayData = data.daily[dateStr];

                    if (dayData) {
                        currentWeekPnl += dayData.pnl;
                        currentWeekTrades += dayData.trades;
                    }

                    let innerHTML = `<div class="date-num">${i}</div>`;
                    let pnlClass = '';
                    let pnlText = '-';

                    if (dayData && dayData.trades > 0) {
                        pnlText = (dayData.pnl > 0 ? '+$' : '-$') + Math.abs(dayData.pnl).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                        pnlClass = dayData.pnl > 0 ? 'win' : 'loss';
                        innerHTML += `<div class="day-pnl ${pnlClass}">${pnlText}</div>`;
                        let tradeStats = '';
                        if (dayData.longs > 0) tradeStats += `<div style="background-color: rgba(26,77,46,0.6); color: #a3e635; border: 1px solid #22c55e; border-radius: 12px; padding: 2px 6px; margin-bottom: 3px; display: flex; justify-content: space-between; font-weight: bold; font-family: monospace; font-size: 11px;"><span>LONG</span><span>${dayData.longs.toString().padStart(2, '0')}</span></div>`;
                        if (dayData.shorts > 0) tradeStats += `<div style="background-color: rgba(74,19,28,0.6); color: #f87171; border: 1px solid #ef4444; border-radius: 12px; padding: 2px 6px; display: flex; justify-content: space-between; font-weight: bold; font-family: monospace; font-size: 11px;"><span>SHORT</span><span>${dayData.shorts.toString().padStart(2, '0')}</span></div>`;
                        if (tradeStats) innerHTML += `<div style="font-size: 10px; font-family: monospace; width: 80%; margin: 4px auto 0 auto; text-align: left;">${tradeStats}</div>`;
                        innerHTML += `<div class="trade-dot"></div>`;
                        window.validTradingDays.push(dateStr);
                    }

                    const div = document.createElement('div');
                    div.className = 'cal-day';
                    div.innerHTML = innerHTML;

                    if (dayData && dayData.trades > 0) {
                        div.onclick = () => {
                            document.querySelectorAll('.cal-day').forEach(el => el.classList.remove('active'));
                            div.classList.add('active');
                            loadDayChart(dateStr);
                        };
                    } else {
                        div.style.cursor = 'default';
                    }

                    grid.appendChild(div);

                    if (new Date(year, monthIdx - 1, i).getDay() === 6) {
                        const wpDiv = document.createElement('div');
                        wpDiv.className = 'cal-day';
                        wpDiv.style.cssText = 'background: rgba(255,255,255,0.05); border-left: 2px solid rgba(255,255,255,0.2); align-items: center; justify-content: center; cursor: default;';
                        const wpClass = currentWeekPnl > 0 ? 'win' : (currentWeekPnl < 0 ? 'loss' : '');
                        const wpText = (currentWeekPnl > 0 ? '+$' : (currentWeekPnl < 0 ? '-$' : '$')) + Math.abs(currentWeekPnl).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                        wpDiv.innerHTML = `<div style="font-size: 0.75rem; font-weight: bold; color: var(--text-secondary); margin-bottom: 5px;">Week PnL</div><div class="day-pnl ${wpClass}" style="margin:0;">${currentWeekTrades > 0 ? wpText : '-'}</div>`;
                        grid.appendChild(wpDiv);
                        currentWeekPnl = 0;
                        currentWeekTrades = 0;
                    }
                }

                const lastDay = new Date(year, monthIdx, 0).getDay();
                if (lastDay !== 6) {
                    for (let i = lastDay + 1; i <= 6; i++) {
                        const emptyDiv = document.createElement('div');
                        emptyDiv.className = 'cal-day empty';
                        grid.appendChild(emptyDiv);
                    }
                    const wpDiv = document.createElement('div');
                    wpDiv.className = 'cal-day';
                    wpDiv.style.cssText = 'background: rgba(255,255,255,0.05); border-left: 2px solid rgba(255,255,255,0.2); align-items: center; justify-content: center; cursor: default;';
                    const wpClass = currentWeekPnl > 0 ? 'win' : (currentWeekPnl < 0 ? 'loss' : '');
                    const wpText = (currentWeekPnl > 0 ? '+$' : (currentWeekPnl < 0 ? '-$' : '$')) + Math.abs(currentWeekPnl).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                    wpDiv.innerHTML = `<div style="font-size: 0.75rem; font-weight: bold; color: var(--text-secondary); margin-bottom: 5px;">Week PnL</div><div class="day-pnl ${wpClass}" style="margin:0;">${currentWeekTrades > 0 ? wpText : '-'}</div>`;
                    grid.appendChild(wpDiv);
                }

                document.getElementById('dayChartEmpty').style.display = 'grid';
                if (priceChart) {
                    priceChart.remove();
                    volumeChart.remove();
                    priceChart = null;
                    volumeChart = null;
                }

                const svgContainer = document.getElementById('drawings-svg');
                if (svgContainer) svgContainer.innerHTML = '';

                const tradesContainer = document.getElementById('dayTradesContainer');
                if (tradesContainer) tradesContainer.innerHTML = '';

                document.getElementById('dayChartArea').style.display = 'none';
                document.getElementById('calendarTopHeader').style.display = 'flex';
                document.getElementById('calendarViewArea').style.display = 'flex';

                document.getElementById('dayChartHeader').querySelector('#dayChartTitle').innerText = 'Select a day';
                document.getElementById('dayChartStats').style.display = 'none';

                if (window.autoLoadDay) {
                    if (window.validTradingDays.length > 0) {
                        let targetDayStr = (window.autoLoadDay === 'first') ? window.validTradingDays[0] : window.validTradingDays[window.validTradingDays.length - 1];

                        document.querySelectorAll('.cal-day').forEach(el => el.classList.remove('active'));
                        const dayNum = parseInt(targetDayStr.split('-')[2], 10);
                        const dayDivs = document.querySelectorAll('.cal-day:not(.empty)');
                        if (dayNum > 0 && dayNum <= dayDivs.length) {
                            dayDivs[dayNum - 1].classList.add('active');
                        }
                        setTimeout(() => loadDayChart(targetDayStr), 50);
                        window.autoLoadDay = null;
                        window.autoLoadDayCount = 0;
                    } else {
                        window.autoLoadDayCount = (window.autoLoadDayCount || 0) + 1;
                        if (window.autoLoadDayCount > 60) {
                            window.autoLoadDay = null;
                            window.autoLoadDayCount = 0;
                        } else {
                            if (window.autoLoadDay === 'first') {
                                setTimeout(window.nextMonth, 50);
                            } else {
                                setTimeout(window.prevMonth, 50);
                            }
                        }
                    }
                } else {
                    window.autoLoadDay = null;
                    window.autoLoadDayCount = 0;
                }

                document.getElementById('calendarModal').style.display = 'flex';
            })
            .catch(err => {
                loadingOverlay.style.display = 'none';
                alert('Error: ' + err);
            });
    };

    function formatEtTime(time) {
        return new Intl.DateTimeFormat('en-US', {
            timeZone: 'America/New_York',
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        }).format(new Date(time * 1000));
    }

    function formatEtDateTime(time) {
        return new Intl.DateTimeFormat('en-US', {
            timeZone: 'America/New_York',
            hour12: false,
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        }).format(new Date(time * 1000)).replace(',', '');
    }

    function loadDayChart(dateStr) {
        window.currentChartRequestDate = dateStr;
        window.currentChartDate = dateStr;
        if (window.validTradingDays) {
            let idx = window.validTradingDays.indexOf(dateStr);
            let btnPrev = document.getElementById('btnPrevDay');
            let btnNext = document.getElementById('btnNextDay');
            if (btnPrev) {
                btnPrev.style.display = 'flex';
                btnPrev.disabled = false; // Always enabled to allow month crossing
            }
            if (btnNext) {
                btnNext.style.display = 'flex';
                btnNext.disabled = false; // Always enabled to allow month crossing
            }
        }

        document.getElementById('calendarTopHeader').style.display = 'none';
        document.getElementById('calendarViewArea').style.display = 'none';
        document.getElementById('dayChartArea').style.display = 'flex';

        // Highlight active calendar day
        document.querySelectorAll('.cal-day').forEach(el => el.classList.remove('active'));
        const dayDivs = document.querySelectorAll('.cal-day:not(.empty)');
        if (dayDivs.length > 0) {
            const dayNum = parseInt(dateStr.split('-')[2], 10);
            if (dayNum > 0 && dayNum <= dayDivs.length) {
                dayDivs[dayNum - 1].classList.add('active');
            }
        }

        const dataPath = currentData;
        document.getElementById('dayChartEmpty').style.display = 'flex';
        document.getElementById('dayChartEmpty').innerText = 'Loading chart...';

        if (priceChart) {
            priceChart.remove();
            volumeChart.remove();
            priceChart = null;
            volumeChart = null;
        }

        fetch('/api/day_chart', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date: dateStr, data_path: dataPath })
        }).then(res => res.json()).then(data => {
            if (window.currentChartRequestDate !== dateStr) return;

            if (data.error) {
                document.getElementById('dayChartEmpty').innerText = data.error;
                return;
            }

            document.getElementById('dayChartEmpty').style.display = 'none';
            document.getElementById('dayChartHeader').querySelector('#dayChartTitle').innerText = `${dateStr}`;

            const totalPnL = data.trades.reduce((sum, t) => sum + t.pnl, 0);
            document.getElementById('dayChartStats').style.display = 'flex';
            document.getElementById('dayChartTrades').innerText = data.trades.length;
            document.getElementById('dayChartPnl').innerText = (totalPnL >= 0 ? '+' : '') + '$' + Math.abs(totalPnL).toFixed(2);
            document.getElementById('dayChartPnl').style.color = totalPnL > 0 ? 'var(--success)' : (totalPnL < 0 ? 'var(--danger)' : '#dbe4f0');

            const tradesContainer = document.getElementById('dayTradesContainer');
            if (tradesContainer) {
                tradesContainer.innerHTML = '';
                data.trades.forEach(t => {
                    const isLong = t.type === 'Long';
                    const pnlColor = t.pnl > 0 ? '#00e676' : '#f06292';

                    const card = document.createElement('div');
                    card.style.display = 'flex';
                    card.style.alignItems = 'center';
                    card.style.background = 'rgba(255,255,255,0.03)';
                    card.style.border = '1px solid rgba(255,255,255,0.07)';
                    card.style.borderRadius = '4px';
                    card.style.padding = '6px 10px';
                    card.style.fontSize = '12px';
                    card.style.fontFamily = 'monospace';
                    card.style.whiteSpace = 'nowrap';
                    card.style.gap = '8px';
                    card.style.flexShrink = '0';

                    const typeBadge = document.createElement('span');
                    typeBadge.innerText = isLong ? 'LONG' : 'SHORT';
                    typeBadge.style.color = isLong ? '#2f7df6' : '#f6c12f';
                    typeBadge.style.fontWeight = 'bold';
                    typeBadge.style.border = `1px solid ${isLong ? 'rgba(47,125,246,0.3)' : 'rgba(246,193,47,0.3)'}`;
                    typeBadge.style.padding = '2px 4px';
                    typeBadge.style.borderRadius = '3px';

                    const details = document.createElement('span');
                    let entryPrice = t.entry_price ? t.entry_price.toFixed(2) : '-';
                    let exitPrice = t.exit_price ? t.exit_price.toFixed(2) : '-';

                    let isWin = t.pnl >= 0;
                    let exitLabel = isWin ? 'TP Exit:' : 'SL Exit:';
                    let exitColor = isWin ? '#00e676' : '#f06292';

                    details.innerHTML = `<span style="color:rgba(255,255,255,0.5)">Entry:</span> <span style="color:#fff">${entryPrice}</span> ` +
                        `<span style="color:${exitColor}; margin-left:8px;">${exitLabel}</span> <span style="color:#fff">${exitPrice}</span>`;

                    const pnlSpan = document.createElement('span');
                    pnlSpan.innerText = (t.pnl > 0 ? '+$' : '-$') + Math.abs(t.pnl).toFixed(2);
                    pnlSpan.style.color = pnlColor;
                    pnlSpan.style.fontWeight = 'bold';
                    pnlSpan.style.marginLeft = '6px';

                    card.appendChild(typeBadge);
                    card.appendChild(details);
                    card.appendChild(pnlSpan);

                    if (t.position_size) {
                        const sizeSpan = document.createElement('span');
                        sizeSpan.innerText = `PT | ${t.position_size}`;
                        sizeSpan.style.color = 'rgba(255,255,255,0.4)';
                        sizeSpan.style.marginLeft = '4px';
                        card.appendChild(sizeSpan);
                    }

                    tradesContainer.appendChild(card);
                });
            }

            const chartOptions = {
                layout: { background: { type: 'solid', color: '#080c12' }, textColor: '#64748b' },
                grid: { vertLines: { color: 'rgba(255,255,255,0.035)' }, horzLines: { color: 'rgba(255,255,255,0.045)' } },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
                timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: true, tickMarkFormatter: time => formatEtTime(time) },
                localization: { timeFormatter: time => formatEtDateTime(time) }
            };

            priceChart = LightweightCharts.createChart(document.getElementById('price-chart'), chartOptions);
            volumeChart = LightweightCharts.createChart(document.getElementById('volume-chart'), chartOptions);

            candleSeries = priceChart.addCandlestickSeries({
                upColor: '#2f7df6', downColor: '#ff3b3b',
                borderUpColor: '#2f7df6', borderDownColor: '#ff3b3b',
                wickUpColor: '#2f7df6', wickDownColor: '#ff3b3b',
                priceFormat: { type: 'price', precision: 2, minMove: 0.25 }
            });

            volumeSeries = volumeChart.addHistogramSeries({
                priceScaleId: '', priceFormat: { type: 'volume' },
                priceLineVisible: false, lastValueVisible: false
            });

            function syncRange(source, targets) {
                source.timeScale().subscribeVisibleLogicalRangeChange(range => {
                    if (!range || dayChartSyncing) return;
                    dayChartSyncing = true;
                    targets.forEach(chart => chart.timeScale().setVisibleLogicalRange(range));
                    dayChartSyncing = false;
                });
            }
            syncRange(priceChart, [volumeChart]);
            syncRange(volumeChart, [priceChart]);

            const vwapSeries = priceChart.addLineSeries({ color: '#d4af37', lineWidth: 2, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false });

            const candles = [];
            const volumes = [];
            const vwaps = [];

            activeDayRowsByTime = new Map();

            data.candles.forEach(row => {
                const up = row.c >= row.o;
                candles.push({
                    time: row.time,
                    open: row.o,
                    high: row.h,
                    low: row.l,
                    close: row.c
                });
                activeDayRowsByTime.set(Math.round(row.time).toString(), row);
                if (row.v !== undefined) {
                    volumes.push({ time: row.time, value: row.v, color: up ? 'rgba(47,125,246,0.75)' : 'rgba(255,59,59,0.75)' });
                }
                if (row.vwap !== undefined) {
                    vwaps.push({ time: row.time, value: row.vwap });
                }
            });

            candleSeries.setData(candles);
            volumeSeries.setData(volumes);
            if (vwaps.length > 0) vwapSeries.setData(vwaps);

            // Native chartMarkers replaced by custom SVG horizontal markers

            // --- TradingView-style TP/SL zones ---
            const drawingsContainer = document.getElementById('drawings-container');
            if (drawingsContainer) drawingsContainer.innerHTML = '';
            const svgContainer = document.getElementById('drawings-svg');
            if (svgContainer) svgContainer.innerHTML = '';

            const tradeDrawingData = [];
            let priceRangeMin = Infinity, priceRangeMax = -Infinity;

            data.trades.forEach(t => {
                const isLong = t.type === 'Long';
                const entryPrice = t.entry_price;
                const exitPrice = t.exit_price;

                let slPrice = t.sl;
                let tpPrice = t.tp;
                let actualGain = isLong ? exitPrice - entryPrice : entryPrice - exitPrice;

                if (!slPrice) {
                    slPrice = isLong ? entryPrice - Math.abs(actualGain) : entryPrice + Math.abs(actualGain);
                }
                if (!tpPrice) {
                    tpPrice = isLong ? entryPrice + Math.abs(entryPrice - slPrice) : entryPrice - Math.abs(entryPrice - slPrice);
                }

                [entryPrice, exitPrice, tpPrice, slPrice].forEach(p => {
                    if (Number.isFinite(p)) {
                        priceRangeMin = Math.min(priceRangeMin, p);
                        priceRangeMax = Math.max(priceRangeMax, p);
                    }
                });

                if (svgContainer) {
                    // Trade path line (diagonal from entry to exit)
                    const tradePathLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                    tradePathLine.setAttribute('data-type', 'trade-path');
                    tradePathLine.setAttribute('data-entry-time', t.entry_time);
                    tradePathLine.setAttribute('data-exit-time', t.exit_time);
                    tradePathLine.setAttribute('data-entry-price', entryPrice);
                    tradePathLine.setAttribute('data-exit-price', exitPrice);
                    tradePathLine.setAttribute('stroke', t.pnl > 0 ? '#00e676' : '#f06292'); // Green for profit, Purple for loss
                    tradePathLine.setAttribute('stroke-width', '1.5');
                    tradePathLine.setAttribute('stroke-dasharray', '4,4');
                    svgContainer.appendChild(tradePathLine);

                    // Create Entry Horizontal Marker (Arrow)
                    const entryArrow = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
                    entryArrow.setAttribute('data-type', 'marker-arrow');
                    entryArrow.setAttribute('data-time', t.entry_time);
                    entryArrow.setAttribute('data-price', entryPrice);
                    entryArrow.setAttribute('fill', isLong ? '#00e676' : '#f06292'); // Green for buy, Purple for sell
                    svgContainer.appendChild(entryArrow);

                    // Create Exit Horizontal Marker (Arrow)
                    const exitArrow = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
                    exitArrow.setAttribute('data-type', 'marker-arrow');
                    exitArrow.setAttribute('data-time', t.exit_time);
                    exitArrow.setAttribute('data-price', exitPrice);
                    exitArrow.setAttribute('fill', !isLong ? '#00e676' : '#f06292'); // Green for buy, Purple for sell
                    svgContainer.appendChild(exitArrow);
                }

                // Horizontal level lines removed per user request
            });

            if (data.candles.length > 0 && Number.isFinite(priceRangeMin)) {
                const rangeExtender = priceChart.addLineSeries({
                    visible: false, lastValueVisible: false,
                    priceLineVisible: false, crosshairMarkerVisible: false
                });
                rangeExtender.setData([
                    { time: data.candles[0].time, value: priceRangeMin },
                    { time: data.candles[data.candles.length - 1].time, value: priceRangeMax }
                ]);
            }

            const candleTimes = data.candles.map(c => c.time);
            function getNearestTime(target) {
                if (!candleTimes.length) return target;
                let l = 0, r = candleTimes.length - 1;
                while (l <= r) {
                    let m = Math.floor((l + r) / 2);
                    if (candleTimes[m] === target) return candleTimes[m];
                    if (candleTimes[m] < target) l = m + 1;
                    else r = m - 1;
                }
                if (l >= candleTimes.length) return candleTimes[candleTimes.length - 1];
                if (r < 0) return candleTimes[0];
                return (target - candleTimes[r] <= candleTimes[l] - target) ? candleTimes[r] : candleTimes[l];
            }

            function updateDrawings() {
                let allPositioned = true;
                const svg = document.getElementById('drawings-svg');
                if (svg) {
                    const elements = svg.querySelectorAll('line, polygon');
                    elements.forEach(el => {
                        const type = el.getAttribute('data-type');

                        // Horizontal lines removed

                        if (type === 'marker-arrow') {
                            const time = Number(el.getAttribute('data-time'));
                            const price = Number(el.getAttribute('data-price'));
                            let x = priceChart.timeScale().timeToCoordinate(getNearestTime(time));
                            let y = candleSeries.priceToCoordinate(price);
                            if (x === null || isNaN(x) || y === null || isNaN(y)) {
                                el.style.display = 'none';
                                allPositioned = false;
                                return;
                            }
                            el.style.display = 'block';
                            // Arrow pointing LEFT exactly at (x, y)
                            el.setAttribute('points', `${x},${y} ${x + 7},${y - 6} ${x + 7},${y - 3} ${x + 18},${y - 3} ${x + 18},${y + 3} ${x + 7},${y + 3} ${x + 7},${y + 6}`);
                            return;
                        }

                        const isTradePath = type === 'trade-path';
                        const entryTime = Number(el.getAttribute('data-entry-time'));
                        const exitTime = Number(el.getAttribute('data-exit-time'));
                        const entryPrice = Number(el.getAttribute('data-entry-price'));

                        let x1 = priceChart.timeScale().timeToCoordinate(getNearestTime(entryTime));
                        let x2 = priceChart.timeScale().timeToCoordinate(getNearestTime(exitTime));
                        let y1 = candleSeries.priceToCoordinate(entryPrice);

                        if (x1 === null || isNaN(x1) || y1 === null || isNaN(y1)) {
                            el.style.display = 'none';
                            allPositioned = false;
                            return;
                        }
                        if (x2 === null || isNaN(x2)) {
                            x2 = priceChart.timeScale().width();
                        }

                        el.style.display = '';

                        if (isTradePath) {
                            let y2 = candleSeries.priceToCoordinate(Number(el.getAttribute('data-exit-price')));
                            if (y2 === null || isNaN(y2)) {
                                el.style.display = 'none';
                                allPositioned = false;
                                return;
                            }
                            el.setAttribute('x1', x1);
                            el.setAttribute('y1', y1);
                            el.setAttribute('x2', x2);
                            el.setAttribute('y2', y2);
                        } else {
                            el.setAttribute('x1', Math.min(x1, x2));
                            el.setAttribute('y1', y1);
                            el.setAttribute('x2', Math.max(x1, x2));
                            el.setAttribute('y2', y1);
                        }
                    });
                }

                return allPositioned;
            }

            priceChart.timeScale().subscribeVisibleLogicalRangeChange(updateDrawings);
            priceChart.subscribeCrosshairMove(updateDrawings);

            // Fit content first, then retry drawing with requestAnimationFrame
            setTimeout(() => {
                dayChartSyncing = true;
                priceChart.timeScale().fitContent();
                volumeChart.timeScale().fitContent();
                priceChart.priceScale('right').applyOptions({ autoScale: true });
                dayChartSyncing = false;

                // Retry loop: keep trying until coordinates resolve (up to 2s)
                let retries = 0;
                function tryDraw() {
                    const success = updateDrawings();
                    if (!success && retries < 120) {
                        retries++;
                        requestAnimationFrame(tryDraw);
                    }
                }
                requestAnimationFrame(tryDraw);
            }, 100);

        }).catch(err => {
            document.getElementById('dayChartEmpty').innerText = 'Error loading day chart: ' + err;
        });
    }

    document.addEventListener('keydown', function (e) {
        const modal = document.getElementById('calendarModal');
        if (modal && modal.style.display === 'flex') {
            const dayChartArea = document.getElementById('dayChartArea');
            if (dayChartArea && dayChartArea.style.display === 'flex') {
                if (e.key === 'ArrowLeft') {
                    if (typeof window.prevDay === 'function') window.prevDay();
                } else if (e.key === 'ArrowRight') {
                    if (typeof window.nextDay === 'function') window.nextDay();
                }
            } else {
                if (e.key === 'ArrowLeft') {
                    if (typeof window.prevMonth === 'function') window.prevMonth();
                } else if (e.key === 'ArrowRight') {
                    if (typeof window.nextMonth === 'function') window.nextMonth();
                }
            }
        }
    });

    // ==========================================
    // RETURN PATH ROBUSTNESS & BLOCK BOOTSTRAP
    // ==========================================
    const btnRunBootstrap = document.getElementById('btnRunBootstrap');
    if (btnRunBootstrap) {
        btnRunBootstrap.addEventListener('click', async function () {
            btnRunBootstrap.disabled = true;
            btnRunBootstrap.innerText = 'Resampling Paths...';

            const method = document.getElementById('bootMethod').value;
            const blockMode = document.getElementById('bootBlockMode').value;
            const blockLength = blockMode === 'auto' ? 'auto' : parseInt(blockMode);
            const numSims = parseInt(document.getElementById('bootSims').value) || 5000;
            const seed = parseInt(document.getElementById('bootSeed').value) || 42;

            try {
                const response = await fetch('/api/run_bootstrap', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        method: method,
                        block_length: blockLength,
                        num_simulations: numSims,
                        seed: seed
                    })
                });


                const data = await response.json();
                if (!response.ok || data.error) {
                    alert('Bootstrap error: ' + (data.error || 'Failed to compute diagnostics'));
                    return;
                }

                renderBootstrapResults(data);
            } catch (err) {
                console.error(err);
                alert('Network or server error during bootstrap: ' + err.message);
            } finally {
                btnRunBootstrap.disabled = false;
                btnRunBootstrap.innerText = 'Run Bootstrap Diagnostics';
            }
        });
    }

    function renderBootstrapResults(data) {
        document.getElementById('bootDiagnosticBanner').style.display = 'block';
        document.getElementById('bootResultsContainer').style.display = 'block';

        const obs = data.observed || {};
        const iid = data.iid_bootstrap || {};
        const stat = data.stationary_bootstrap || {};
        const comp = data.comparative || {};
        const meta = data.metadata || {};

        // 1. Badges
        const ratingBadge = document.getElementById('bootRatingBadge');
        const penaltyBadge = document.getElementById('bootPenaltyBadge');
        const blockBadge = document.getElementById('bootBlockBadge');

        const rating = comp.path_robustness_heuristic || 'N/A';
        ratingBadge.innerText = rating;
        ratingBadge.style.color = rating === 'ROBUST' ? 'var(--success)' : (rating === 'MODERATE' ? 'var(--accent)' : 'var(--danger)');

        const delta95 = comp.signed_dependence_dd_delta_95;
        if (delta95 !== undefined) {
            penaltyBadge.innerText = (delta95 >= 0 ? `+${delta95.toFixed(2)}%` : `${delta95.toFixed(2)}%`);
            penaltyBadge.style.color = delta95 > 0 ? 'var(--danger)' : 'var(--success)';
        } else {
            penaltyBadge.innerText = '--';
        }
        blockBadge.innerText = `${meta.block_length || '--'} Days (${meta.block_length_source || 'AUTO'})`;


        // 2. Table Rows
        const rows = [
            {
                metric: 'Realized Sharpe Ratio',
                obs: obs.sharpe !== undefined ? obs.sharpe.toFixed(2) : '--',
                iid: iid.sharpe_median !== undefined ? `${iid.sharpe_median.toFixed(2)} <span style="color: var(--text-dim); font-size: 0.85em;">[${iid.sharpe_p5} to ${iid.sharpe_p95}]</span>` : '--',
                stat: stat.sharpe_median !== undefined ? `${stat.sharpe_median.toFixed(2)} <span style="color: var(--text-dim); font-size: 0.85em;">[${stat.sharpe_p5} to ${stat.sharpe_p95}]</span>` : '--'
            },
            {
                metric: 'Probability of Non-Positive Sharpe (P(SR ≤ 0))',
                obs: '—',
                iid: iid.p_sharpe_le_zero !== undefined ? `${iid.p_sharpe_le_zero.toFixed(2)}%` : '--',
                stat: stat.p_sharpe_le_zero !== undefined ? `<span style="color: ${stat.p_sharpe_le_zero > 10 ? 'var(--danger)' : 'var(--success)'}; font-weight: 600;">${stat.p_sharpe_le_zero.toFixed(2)}%</span>` : '--'
            },
            {
                metric: 'Maximum Drawdown (95th %ile Stress Risk)',
                obs: obs.max_dd !== undefined ? `-${obs.max_dd.toFixed(2)}%` : '--',
                iid: iid.max_dd_p95 !== undefined ? `-${iid.max_dd_p95.toFixed(2)}%` : '--',
                stat: stat.max_dd_p95 !== undefined ? `<span style="color: var(--danger); font-weight: 600;">-${stat.max_dd_p95.toFixed(2)}%</span>` : '--'
            },
            {
                metric: 'Tail Drawdown (99th %ile Black Swan)',
                obs: '—',
                iid: iid.max_dd_p99 !== undefined ? `-${iid.max_dd_p99.toFixed(2)}%` : '--',
                stat: stat.max_dd_p99 !== undefined ? `-${stat.max_dd_p99.toFixed(2)}%` : '--'
            },
            {
                metric: 'Max Drawdown Duration (95th %ile)',
                obs: obs.max_dd_duration !== undefined ? `${obs.max_dd_duration} d` : '--',
                iid: iid.dd_duration_p95 !== undefined ? `${iid.dd_duration_p95} d` : '--',
                stat: stat.dd_duration_p95 !== undefined ? `${stat.dd_duration_p95} d` : '--'
            },
            {
                metric: 'Recovery Time from Trough (95th %ile)',
                obs: '—',
                iid: iid.recovery_time_p95 !== undefined ? `${iid.recovery_time_p95} d` : '--',
                stat: stat.recovery_time_p95 !== undefined ? `${stat.recovery_time_p95} d` : '--'
            },
            {
                metric: 'Unrecovered Horizon Probability',
                obs: '—',
                iid: iid.unrecovered_probability !== undefined ? `${iid.unrecovered_probability.toFixed(2)}%` : '--',
                stat: stat.unrecovered_probability !== undefined ? `${stat.unrecovered_probability.toFixed(2)}%` : '--'
            },
            {
                metric: 'Severe Drawdown Risk: P(Max DD ≥ 20%)',
                obs: '—',
                iid: iid.threshold_probabilities ? `${iid.threshold_probabilities['P(Max DD >= 20%)'] || 0}%` : '--',
                stat: stat.threshold_probabilities ? `${stat.threshold_probabilities['P(Max DD >= 20%)'] || 0}%` : '--'
            }
        ];

        let html = '';
        rows.forEach(r => {
            html += `<tr>
                <td style="font-weight: 500;">${r.metric}</td>
                <td>${r.obs}</td>
                <td>${r.iid}</td>
                <td>${r.stat}</td>
            </tr>`;
        });
        document.getElementById('bootTableBody').innerHTML = html;

        // 3. Plotly Fan Chart
        const fan = (stat.fan_chart || iid.fan_chart);
        if (fan && fan.p50 && fan.p50.length > 0) {
            const chartDiv = document.getElementById('plotBootstrapFanChart');
            chartDiv.style.display = 'block';

            const x = Array.from({ length: fan.p50.length }, (_, i) => `Day ${i}`);

            const traces = [
                // 95th Percentile upper
                { x: x, y: fan.p95, mode: 'lines', line: { color: 'rgba(56, 189, 248, 0.0)' }, showlegend: false, hoverinfo: 'skip' },
                // 5th Percentile lower with fill to 95th
                { x: x, y: fan.p5, mode: 'lines', fill: 'tonexty', fillcolor: 'rgba(56, 189, 248, 0.12)', line: { color: 'rgba(56, 189, 248, 0.0)' }, name: '5% - 95% Envelope' },

                // 75th Percentile upper
                { x: x, y: fan.p75, mode: 'lines', line: { color: 'rgba(56, 189, 248, 0.0)' }, showlegend: false, hoverinfo: 'skip' },
                // 25th Percentile lower with fill to 75th
                { x: x, y: fan.p25, mode: 'lines', fill: 'tonexty', fillcolor: 'rgba(56, 189, 248, 0.22)', line: { color: 'rgba(56, 189, 248, 0.0)' }, name: '25% - 75% Envelope' },

                // 50th Percentile Median
                { x: x, y: fan.p50, mode: 'lines', line: { color: '#38bdf8', width: 2.5 }, name: 'Median Resampled Path' }
            ];

            if (obs.curve_pct && obs.curve_pct.length > 0) {
                const xObs = Array.from({ length: obs.curve_pct.length }, (_, i) => `Day ${i}`);
                traces.push({
                    x: xObs, y: obs.curve_pct, mode: 'lines', line: { color: '#10b981', width: 2, dash: 'dot' }, name: 'Observed Strategy'
                });
            }

            const layout = {
                title: 'Stationary Bootstrap Return Path Fan Chart (% Equity)',
                plot_bgcolor: '#0e1117',
                paper_bgcolor: '#0e1117',
                font: { color: '#fafafa' },
                margin: { l: 50, r: 20, t: 40, b: 50 },
                xaxis: { title: 'Trading Days', showgrid: true, gridcolor: 'rgba(255,255,255,0.03)' },
                yaxis: { title: 'Cumulative Return (%)', showgrid: true, gridcolor: 'rgba(255,255,255,0.03)' },
                hovermode: 'x',
                legend: { orientation: 'h', y: -0.15 }
            };

            plotlyRedraw('plotBootstrapFanChart', traces, layout, { responsive: true });
        }
    }

});

