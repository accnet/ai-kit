  (async () => {

    // ── LOAD DATA ──────────────────────────────────────────────
    let board = {todo:[],['in-progress']:[],['implementation-complete']:[],['qa-passed']:[],['review-approved']:[],done:[],blocked:[]};
    let arch = {};
    let impact = {};
    let events = [];
    let discovered = null; // discovered-architecture.json — optional, may not exist yet
    let discoveryWarnings = [];
    let architectureEdges = [];
    let taskModuleMap = {};
    let projectData = {};
    let contractsData = {items:[], edges:[]};
    let ownershipData = {owners:{}, unowned:[]};
    let risksData = {items:[]};
    let evidenceData = {items:[]};
    let taskItemsData = [];
    let gitData = {};
    let artifactManifest = null;
    let artifactMode = 'loading';
    let hasLoaded = false;
    let sourceFilter = 'all'; // 'all' | 'declared' | 'discovered'
    let ownerFilterVal = 'all';
    let contextFilterVal = 'all';

    const ARTIFACT_FILES = [
      'project.json','architecture.json','modules.json','dependencies.json','contracts.json','tasks.json',
      'dag.json','ownership.json','risks.json','git.json','evidence.json','events.json'
    ];

    async function loadCanonicalArtifacts(attempt = 0) {
      try {
        const manifestResponse = await fetch('/artifacts/project/manifest.json', {cache:'no-store'});
        if (!manifestResponse.ok) throw new Error(`manifest HTTP ${manifestResponse.status}`);
        const manifest = await manifestResponse.json();
        if (manifest.schema_version !== 1 || manifest.artifact_set_version !== 1) throw new Error('unsupported artifact manifest');
        const declared = Object.keys(manifest.artifacts || {}).sort();
        if (declared.join('|') !== [...ARTIFACT_FILES].sort().join('|')) throw new Error('artifact manifest is incomplete');
        const values = await Promise.all(ARTIFACT_FILES.map(async filename => {
          const response = await fetch(`/artifacts/project/${filename}`, {cache:'no-store'});
          if (!response.ok) throw new Error(`${filename} HTTP ${response.status}`);
          return response.json();
        }));
        const bundle = Object.fromEntries(ARTIFACT_FILES.map((filename,index)=>[filename,values[index]]));
        for (const [filename,payload] of Object.entries(bundle)) {
          if (payload.schema_version !== 1 || payload.generation_id !== manifest.generation_id || payload.artifact !== filename.replace('.json','')) {
            throw new Error(`mixed or unsupported artifact generation: ${filename}`);
          }
        }

        const moduleItems = bundle['modules.json'].data.items || [];
        const moduleNames = Object.fromEntries(moduleItems.map(item=>[item.id,item.name]));
        const nextArch = {};
        const nextTaskModuleMap = {};
        moduleItems.forEach(item => {
          nextArch[item.name] = {
            path:item.path, owner:item.owner, source:item.source, kind:item.kind,
            parent:item.parent_ref ? item.parent_ref.replace(/^context:/,'') : null,
            framework:item.framework, confidence:item.observation?.confidence,
            observation:item.observation || {classification:'observed',confidence:1},
            related_tasks:(item.task_refs||[]).map(ref=>ref.split(':').at(-1)),
            depends_on:(item.depends_on||[]).map(ref=>moduleNames[ref]).filter(Boolean),
          };
          (item.task_refs||[]).forEach(ref=>{ nextTaskModuleMap[ref.split(':').at(-1)] = item.name; });
        });
        const nextEdges = (bundle['dependencies.json'].data.items || []).map(edge=>({
          from:moduleNames[edge.from], to:moduleNames[edge.to], active:edge.active,
          kind:edge.kind, observation:edge.observation
        })).filter(edge=>edge.from && edge.to);
        const nextImpact = {};
        Object.entries(bundle['architecture.json'].data.impact || {}).forEach(([moduleRef,value]) => {
          const name = moduleNames[moduleRef];
          if (!name) return;
          nextImpact[name] = {
            direct_dependents:(value.direct_dependents||[]).map(ref=>moduleNames[ref]).filter(Boolean),
            all_dependents:(value.all_dependents||[]).map(ref=>moduleNames[ref]).filter(Boolean),
            affected_tasks:(value.affected_task_refs||[]).map(ref=>ref.split(':').at(-1)),
          };
        });

        // Commit only after the whole generation validates. A failed refresh
        // leaves the previous render intact until a complete manifest appears.
        artifactManifest = manifest;
        projectData = bundle['project.json'].data;
        contractsData = bundle['contracts.json'].data;
        ownershipData = bundle['ownership.json'].data;
        risksData = bundle['risks.json'].data;
        evidenceData = bundle['evidence.json'].data;
        taskItemsData = bundle['tasks.json'].data.items || [];
        gitData = bundle['git.json'].data;
        board = bundle['tasks.json'].data.board;
        events = bundle['events.json'].data.events || [];
        arch = nextArch;
        impact = nextImpact;
        architectureEdges = nextEdges;
        taskModuleMap = nextTaskModuleMap;
        discoveryWarnings = (risksData.items||[]).filter(item=>item.source==='architecture-discovery');
        artifactMode = 'canonical';
        hasLoaded = true;
        return true;
      } catch (error) {
        if (attempt === 0) {
          await new Promise(resolve=>setTimeout(resolve,80));
          return loadCanonicalArtifacts(1);
        }
        console.warn('Canonical artifact bundle unavailable:', error);
        return false;
      }
    }

    async function loadLegacyArtifacts() {
      try {
        const [b,a,im,ev,discoveryResponse] = await Promise.all([
          fetch('board.json').then(r=>r.json()),
          fetch('architecture.json').then(r=>r.json()),
          fetch('impact.json').then(r=>r.json()),
          fetch('events.json').then(r=>r.json()),
          fetch('discovered-architecture.json').catch(()=>null),
        ]);
        board = b; arch = a; impact = im; events = ev;
        discovered = discoveryResponse?.ok ? await discoveryResponse.json() : null;
        architectureEdges = []; taskModuleMap = {};
        mergeDiscoveredArchitecture();
        projectData = {}; contractsData = {items:[],edges:[]}; ownershipData = {owners:{},unowned:[]}; taskItemsData = [];
        risksData = {items:[]}; evidenceData = {items:[]}; gitData = {};
        artifactManifest = null; artifactMode = 'legacy'; hasLoaded = true;
        return true;
      } catch(error) {
        console.warn('Legacy visualizer projection unavailable:', error);
        return false;
      }
    }

    async function loadData() {
      if (await loadCanonicalArtifacts()) return true;
      if (hasLoaded) return false;
      return loadLegacyArtifacts();
    }

    // ── MERGE DECLARED + DISCOVERED ARCHITECTURE ──────────────
    // Combines architecture.json (declared bounded contexts, always
    // present) with discovered-architecture.json (feature modules +
    // dependency edges, optional) into the single `arch` map every render
    // function below already reads generically by key — no module name is
    // ever hardcoded here or anywhere else in this file.
    function mergeDiscoveredArchitecture() {
      const merged = {};
      Object.entries(arch).forEach(([name, info]) => {
        merged[name] = Object.assign({source: 'declared', kind: 'bounded-context', parent: null, confidence: 1,
          observation:{classification:'observed',source_kind:'config',confidence:1}}, info);
      });
      discoveryWarnings = (discovered && discovered.warnings) || [];
      if (discovered && discovered.modules) {
        Object.entries(discovered.modules).forEach(([name, info]) => {
          if (info.source === 'declared') return; // already present from architecture.json
          if (!merged[name]) {
            merged[name] = Object.assign({depends_on: [], observation:{classification:'inferred',source_kind:'convention',confidence:info.confidence||.5}}, info);
          }
        });
        (discovered.edges || []).forEach(edge => {
          if (!merged[edge.from]) return;
          const deps = merged[edge.from].depends_on || (merged[edge.from].depends_on = []);
          if (!deps.includes(edge.to)) deps.push(edge.to);
          architectureEdges.push({from:edge.from,to:edge.to,active:true,kind:edge.kind,
            observation:{classification:edge.kind==='declared'?'observed':'inferred',confidence:edge.confidence||.5}});
        });
      }
      arch = merged;
    }
    await loadData();

    // ── STATE ──────────────────────────────────────────────────
    let selectedMod = null;
    let currentTab = 'info';
    let inspMini = false;
    let zoomScale = 1.0;
    let panX = 0, panY = 0;
    let isPanning = false;
    let panStartX = 0, panStartY = 0, panOriginX = 0, panOriginY = 0;
    let isPlaying = false;
    let playInterval = null;
    let currentLayout = 'ltr'; // 'ltr' | 'radial'
    let layers = { arch: true, impact: false, owner: true, rev: false, evolution: true, labels: true };
    let searchQ = '';
    const collapsedOwnerGroups = new Set();
    let replayTimestamp = null;
    const replaySeenModules = new Set();

    // ── DOM REFS ───────────────────────────────────────────────
    const sidebarLeft = document.getElementById('sidebarLeft');
    const sidebarRight = document.getElementById('sidebarRight');
    const btnToggleLeft = document.getElementById('btnToggleLeft');
    const btnToggleRight = document.getElementById('btnToggleRight');
    const nodesLayer = document.getElementById('nodesLayer');
    const archSvg = document.getElementById('archSvg');
    const inspBody = document.getElementById('inspBody');
    const inspTabs = document.getElementById('inspTabs');
    const inspector = document.getElementById('inspector');
    const btnInspToggle = document.getElementById('btnInspToggle');
    const searchInput = document.getElementById('searchInput');
    const lblZoom = document.getElementById('lblZoom');
    const modTree = document.getElementById('modTree');
    const modCount = document.getElementById('modCount');
    const modFilterBar = document.getElementById('modFilterBar');
    const ownerFilterSel = document.getElementById('ownerFilterSel');
    const contextFilterSel = document.getElementById('contextFilterSel');
    const timelineSlider = document.getElementById('timelineSlider');
    const tcLabel = document.getElementById('tcLabel');
    const btnPlay = document.getElementById('btnPlay');
    const playIcon = document.getElementById('playIcon');
    const selCard = document.getElementById('selCard');
    const agentList = document.getElementById('agentList');
    const eventList = document.getElementById('eventList');
    const evCount = document.getElementById('evCount');
    const statsRow = document.getElementById('statsRow');
    const evoBoard = document.getElementById('evoBoard');
    const runtimeBody = document.getElementById('runtimeBody');
    const replayBody = document.getElementById('replayBody');
    const canvasWrap = document.getElementById('canvasWrap');
    const minimapCanvas = document.getElementById('minimapCanvas');
    const minimapView = document.getElementById('minimapView');
    const artifactStatus = document.getElementById('artifactStatus');
    const projectOverview = document.getElementById('projectOverview');
    const projectRisks = document.getElementById('projectRisks');
    const contractSummary = document.getElementById('contractSummary');
    const contractGraph = document.getElementById('contractGraph');
    const contractBody = document.getElementById('contractBody');

    // ── HELPERS ────────────────────────────────────────────────
    const ownerColors = { backend: '#60a5fa', frontend: '#a78bfa', devops: '#2dd4bf', qa: '#f59e0b' };
    function ownerColor(o) { return ownerColors[o] || '#94a3b8'; }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, char=>({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
      })[char]);
    }

    function timeAgo(iso) {
      const diff = Date.now() - new Date(iso).getTime();
      if (diff < 60000) return Math.floor(diff/1000)+'s ago';
      if (diff < 3600000) return Math.floor(diff/60000)+'m ago';
      return Math.floor(diff/3600000)+'h ago';
    }

    function formatTs(iso) {
      return new Date(iso).toLocaleTimeString('vi-VN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    }

    const allTasks = () => Object.values(board).flat();
    const allMods = () => Object.keys(arch).filter(passesModuleFilters);
    function passesModuleFilters(k) {
      const m = arch[k] || {};
      if (sourceFilter !== 'all' && (m.source || 'declared') !== sourceFilter) return false;
      if (ownerFilterVal !== 'all' && (m.owner || 'unknown') !== ownerFilterVal) return false;
      if (contextFilterVal !== 'all' && k !== contextFilterVal && (m.parent || null) !== contextFilterVal) return false;
      return true;
    }
    function replayBounds() {
      const timestamps = events.map(e => Date.parse(e.ts)).filter(Number.isFinite);
      return timestamps.length ? { min: Math.min(...timestamps), max: Math.max(...timestamps) } : null;
    }
    function isReplayMode() { return replayTimestamp !== null; }
    function replayEvents() {
      return events.filter(e => !isReplayMode() || Date.parse(e.ts) <= replayTimestamp);
    }
    function taskForId(id) { return allTasks().find(task => task.id === id); }
    function moduleForTask(task) {
      if (!task) return null;
      if (artifactMode === 'canonical') return taskModuleMap[task.id] || null;
      if (task.context && arch[task.context]) return task.context;
      const files = task.files || [];
      if (!files.length) return null;
      // Priority 2: most specific module whose path is a prefix of (or
      // fnmatch-style glob-matches, `*` spans `/`) one of the task's files.
      // Never a bare substring/name comparison, which could match an
      // unrelated module that happens to share a word.
      let best = null;
      Object.entries(arch).forEach(([name, module]) => {
        const path = module.path;
        if (!path) return;
        const globRe = new RegExp('^' + path.split('*').map(s => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
        const matches = files.some(f => f === path || f.startsWith(path + '/') || globRe.test(f));
        if (matches && (!best || path.length > arch[best].path.length)) best = name;
      });
      return best;
    }
    function moduleTasks(name) { return allTasks().filter(task => moduleForTask(task) === name); }
    function taskAddedAtReplay(task) {
      return replayEvents().some(event => event.task === task.id && event.action === 'add-task');
    }
    function moduleVisibleAtReplay(name) {
      if (!isReplayMode()) return true;
      const related = moduleTasks(name);
      return !related.length || related.some(taskAddedAtReplay);
    }
    function moduleStats(name) {
      const related = moduleTasks(name).filter(task => !isReplayMode() || taskAddedAtReplay(task));
      const total = related.reduce((sum, task) => sum + Number(task.acceptance_count || (task.acceptance || []).length), 0);
      const passed = related.reduce((sum, task) => {
        const latest = replayEvents().filter(event => event.task === task.id && event.to).at(-1);
        const status = isReplayMode() ? latest?.to : task.status;
        return sum + (status === 'done' ? Number(task.acceptance_count || (task.acceptance || []).length) : 0);
      }, 0);
      return { total, passed, ratio: total ? passed / total : 0 };
    }

    // ── SIDEBAR TOGGLE ─────────────────────────────────────────
    btnToggleLeft.addEventListener('click', () => sidebarLeft.classList.toggle('collapsed'));
    btnToggleRight.addEventListener('click', () => sidebarRight.classList.toggle('collapsed'));

    // ── INSPECTOR TOGGLE ──────────────────────────────────────
    btnInspToggle.addEventListener('click', () => {
      inspMini = !inspMini;
      inspector.classList.toggle('mini', inspMini);
      btnInspToggle.textContent = inspMini ? '▼' : '▲';
    });

    // ── ZOOM & PAN ────────────────────────────────────────────
    function applyZoom() {
      lblZoom.textContent = Math.round(zoomScale*100)+'%';
      const t = `translate(${panX}px, ${panY}px) scale(${zoomScale})`;
      nodesLayer.style.transform = t;
      archSvg.style.transform = t;
      updateMinimap();
    }

    // ── MINIMAP ───────────────────────────────────────────────
    const MINIMAP_NODE_W = 185, MINIMAP_NODE_H = 90;
    function updateMinimap() {
      const mods = allMods().filter(moduleVisibleAtReplay);
      const pos = getPositions();
      minimapCanvas.querySelectorAll('.minimap-node').forEach(n => n.remove());
      const xs = mods.map(k => pos[k]?.x).filter(Number.isFinite);
      const ys = mods.map(k => pos[k]?.y).filter(Number.isFinite);
      if (!xs.length) { minimapView.style.display = 'none'; return; }
      minimapView.style.display = '';

      const minX = Math.min(...xs), maxX = Math.max(...xs) + MINIMAP_NODE_W;
      const minY = Math.min(...ys), maxY = Math.max(...ys) + MINIMAP_NODE_H;
      const contentW = Math.max(1, maxX - minX);
      const contentH = Math.max(1, maxY - minY);

      mods.forEach(k => {
        const p = pos[k];
        if (!p) return;
        const dot = document.createElement('div');
        dot.className = 'minimap-node';
        dot.style.left = (((p.x + MINIMAP_NODE_W/2) - minX) / contentW * 100) + '%';
        dot.style.top = (((p.y + MINIMAP_NODE_H/2) - minY) / contentH * 100) + '%';
        dot.style.background = ownerColor(arch[k]?.owner);
        minimapCanvas.appendChild(dot);
      });

      const rect = canvasWrap.getBoundingClientRect();
      const viewW = rect.width / zoomScale;
      const viewH = rect.height / zoomScale;
      const viewX = -panX / zoomScale;
      const viewY = -panY / zoomScale;
      minimapView.style.left = Math.max(0, Math.min(100, ((viewX - minX) / contentW) * 100)) + '%';
      minimapView.style.top = Math.max(0, Math.min(100, ((viewY - minY) / contentH) * 100)) + '%';
      minimapView.style.width = Math.max(4, Math.min(100, (viewW / contentW) * 100)) + '%';
      minimapView.style.height = Math.max(4, Math.min(100, (viewH / contentH) * 100)) + '%';
    }
    document.getElementById('btnZoomIn').addEventListener('click', () => { zoomScale = Math.min(2, zoomScale+0.15); applyZoom(); });
    document.getElementById('btnZoomOut').addEventListener('click', () => { zoomScale = Math.max(0.4, zoomScale-0.15); applyZoom(); });
    document.getElementById('btnZoomReset').addEventListener('click', () => { zoomScale=1; panX=0; panY=0; applyZoom(); });

    canvasWrap.addEventListener('wheel', e => {
      e.preventDefault();
      const rect = canvasWrap.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;
      const worldX = (mouseX - panX) / zoomScale;
      const worldY = (mouseY - panY) / zoomScale;
      zoomScale = Math.min(2, Math.max(0.4, zoomScale + (e.deltaY < 0 ? 0.1 : -0.1)));
      panX = mouseX - worldX * zoomScale;
      panY = mouseY - worldY * zoomScale;
      applyZoom();
    }, { passive: false });

    canvasWrap.addEventListener('mousedown', e => {
      if (e.target !== canvasWrap && e.target !== nodesLayer && e.target !== archSvg) return;
      isPanning = true;
      panStartX = e.clientX; panStartY = e.clientY;
      panOriginX = panX; panOriginY = panY;
      canvasWrap.classList.add('panning');
    });
    window.addEventListener('mousemove', e => {
      if (!isPanning) return;
      panX = panOriginX + (e.clientX - panStartX);
      panY = panOriginY + (e.clientY - panStartY);
      applyZoom();
    });
    window.addEventListener('mouseup', () => {
      if (!isPanning) return;
      isPanning = false;
      canvasWrap.classList.remove('panning');
    });

    // ── REFRESH ───────────────────────────────────────────────
    document.getElementById('btnRefresh').addEventListener('click', async () => {
      await loadData();
      renderAll();
    });

    // ── SEARCH ────────────────────────────────────────────────
    searchInput.addEventListener('input', e => {
      searchQ = e.target.value.toLowerCase().trim();
      applySearch();
    });
    window.addEventListener('keydown', e => {
      if ((e.ctrlKey||e.metaKey) && e.key==='k') { e.preventDefault(); searchInput.focus(); }
      if (e.key==='Escape') { searchInput.value=''; searchQ=''; applySearch(); searchInput.blur(); }
    });

    function applySearch() {
      document.querySelectorAll('.node-card').forEach(card => {
        const mk = card.dataset.mod.toLowerCase();
        const name = (card.querySelector('.node-name')?.textContent||'').toLowerCase();
        if (!searchQ) { card.classList.remove('match','dimmed'); return; }
        if (mk.includes(searchQ)||name.includes(searchQ)) { card.classList.add('match'); card.classList.remove('dimmed'); }
        else { card.classList.add('dimmed'); card.classList.remove('match'); }
      });
      applyModTreeSearch();
    }

    // ── VIEW TABS ─────────────────────────────────────────────
    document.querySelectorAll('.view-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.view-tab').forEach(t=>t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('.view-panel').forEach(p=>p.classList.remove('active'));
        const id = 'view'+tab.dataset.view.charAt(0).toUpperCase()+tab.dataset.view.slice(1);
        document.getElementById(id)?.classList.add('active');
      });
    });

    // ── LAYER STATE (shared by canvas chips + sidebar toggles) ─
    const chipLayerMap = { deps: 'arch', heatmap: 'rev', labels: 'labels' };
    function setLayer(key, on) {
      layers[key] = on;
      document.querySelectorAll(`.layer-toggle[data-layer="${key}"]`).forEach(t => t.classList.toggle('on', on));
      Object.entries(chipLayerMap).forEach(([chipKey, layerKey]) => {
        if (layerKey === key) {
          const chip = document.querySelector(`.chip[data-chip="${chipKey}"]`);
          if (chip) chip.classList.toggle('on', on);
        }
      });
    }

    // ── CANVAS CHIPS ─────────────────────────────────────────
    document.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const c = chip.dataset.chip;
        if (c==='ltr' || c==='radial') {
          currentLayout = c;
          document.querySelectorAll('.chip').forEach(x => {
            if (x.dataset.chip==='ltr'||x.dataset.chip==='radial') x.classList.remove('on');
          });
          chip.classList.add('on');
          renderCanvas();
        } else {
          const layer = chipLayerMap[c];
          if (layer) setLayer(layer, !layers[layer]);
          renderCanvas();
        }
      });
    });

    // ── LAYER TOGGLES ─────────────────────────────────────────
    document.querySelectorAll('.layer-toggle').forEach(tog => {
      tog.addEventListener('click', () => {
        setLayer(tog.dataset.layer, !layers[tog.dataset.layer]);
        renderCanvas();
      });
    });

    // ── INSPECTOR TABS ────────────────────────────────────────
    inspTabs.addEventListener('click', e => {
      const tab = e.target.closest('.insp-tab');
      if (!tab) return;
      document.querySelectorAll('.insp-tab').forEach(t=>t.classList.remove('active'));
      tab.classList.add('active');
      currentTab = tab.dataset.itab;
      renderInspector();
    });

    // ── NODE POSITIONS ────────────────────────────────────────
    function getLayeredPositions() {
      const mods = allMods();
      const depth = {};
      const visiting = new Set();
      function getDepth(name) {
        if (depth[name] !== undefined) return depth[name];
        if (visiting.has(name)) return 0;
        visiting.add(name);
        const deps = (arch[name]?.depends_on || []).filter(dep => arch[dep]);
        depth[name] = deps.length ? Math.max(...deps.map(getDepth)) + 1 : 0;
        visiting.delete(name);
        return depth[name];
      }
      mods.forEach(getDepth);
      const columns = {};
      mods.forEach(name => (columns[depth[name]] ||= []).push(name));
      const positions = {};
      Object.values(columns).forEach(column => column.sort().forEach((name, row) => {
        positions[name] = { x: depth[name] * 250 + 50, y: row * 150 + 50 };
      }));
      return positions;
    }

    function getRadialPositions() {
      const mods = allMods();
      const cx = 500, cy = 280, r = 240;
      const pos = {};
      mods.forEach((k, i) => {
        const a = (2 * Math.PI * i / mods.length) - Math.PI/2;
        pos[k] = { x: Math.round(cx + r * Math.cos(a) - 80), y: Math.round(cy + r * Math.sin(a) - 40) };
      });
      return pos;
    }

    function getPositions() {
      return currentLayout === 'ltr' ? getLayeredPositions() : getRadialPositions();
    }

    // ── MODULE FILTERS (source / owner / context) ─────────────
    modFilterBar.addEventListener('click', e => {
      const btn = e.target.closest('[data-src-filter]');
      if (!btn) return;
      sourceFilter = btn.dataset.srcFilter;
      modFilterBar.querySelectorAll('[data-src-filter]').forEach(b => b.classList.toggle('on', b === btn));
      renderAll();
    });
    ownerFilterSel.addEventListener('change', () => { ownerFilterVal = ownerFilterSel.value; renderAll(); });
    contextFilterSel.addEventListener('change', () => { contextFilterVal = contextFilterSel.value; renderAll(); });

    function refreshFilterOptions() {
      const owners = Array.from(new Set(Object.values(arch).map(m => m.owner).filter(Boolean))).sort();
      const contexts = Array.from(new Set(
        Object.entries(arch).filter(([,m]) => (m.kind||'bounded-context') === 'bounded-context').map(([name]) => name)
      )).sort();
      const rebuild = (sel, values, current) => {
        sel.innerHTML = `<option value="all">All ${sel === ownerFilterSel ? 'owners' : 'contexts'}</option>` +
          values.map(v => `<option value="${v}">${v}</option>`).join('');
        sel.value = values.includes(current) ? current : 'all';
      };
      rebuild(ownerFilterSel, owners, ownerFilterVal);
      rebuild(contextFilterSel, contexts, contextFilterVal);
    }

    // ── RENDER MODULE TREE ────────────────────────────────────
    function renderModTree() {
      const mods = allMods();
      modCount.textContent = mods.length;
      modTree.innerHTML = '';
      // Group by owner
      const groups = {};
      mods.forEach(k => {
        const o = arch[k]?.owner || 'unknown';
        if (!groups[o]) groups[o] = [];
        groups[o].push(k);
      });
      Object.entries(groups).forEach(([owner, list]) => {
        const collapsed = collapsedOwnerGroups.has(owner);
        const groupHead = document.createElement('div');
        groupHead.className = 'mod-group-head';
        groupHead.dataset.owner = owner;
        groupHead.innerHTML = `<span class="mod-group-chevron">${collapsed ? '▸' : '▾'}</span><span style="color:${ownerColor(owner)}">@${owner}</span><span class="mod-group-count">${list.length}</span>`;
        groupHead.addEventListener('click', () => {
          if (collapsedOwnerGroups.has(owner)) collapsedOwnerGroups.delete(owner);
          else collapsedOwnerGroups.add(owner);
          renderModTree();
        });
        modTree.appendChild(groupHead);

        const groupBody = document.createElement('div');
        groupBody.className = 'mod-group-body';
        groupBody.dataset.owner = owner;
        if (collapsed) groupBody.style.display = 'none';

        list.forEach(k => {
          const item = document.createElement('div');
          item.className = 'mod-item' + (selectedMod===k?' active':'');
          item.dataset.mod = k;
          const dot = document.createElement('div');
          dot.className = 'mod-owner-dot';
          dot.style.background = ownerColor(owner);
          const name = document.createElement('div');
          name.className = 'mod-name';
          name.textContent = k.replace(/^(backend|extension)-/,'');
          const m = arch[k] || {};
          if (m.parent) name.textContent = '↳ ' + name.textContent;
          const srcTag = document.createElement('span');
          srcTag.className = 'mod-src-tag ' + (m.source === 'discovered' ? 'mod-src-discovered' : 'mod-src-declared');
          srcTag.textContent = m.source === 'discovered' ? 'discovered' : 'declared';
          name.appendChild(srcTag);
          const path = document.createElement('div');
          path.className = 'mod-path';
          path.textContent = arch[k]?.path || '';
          const inner = document.createElement('div');
          inner.style.flex = '1';
          if (m.parent) inner.style.marginLeft = '10px';
          inner.appendChild(name);
          inner.appendChild(path);
          item.appendChild(dot);
          item.appendChild(inner);
          item.addEventListener('click', () => selectModule(k));
          groupBody.appendChild(item);
        });
        modTree.appendChild(groupBody);
      });
      applyModTreeSearch();
    }

    // ── MOD TREE SEARCH FILTER ─────────────────────────────────
    function applyModTreeSearch() {
      document.querySelectorAll('.mod-group-head').forEach(head => {
        const owner = head.dataset.owner;
        const body = modTree.querySelector(`.mod-group-body[data-owner="${CSS.escape(owner)}"]`);
        if (!body) return;
        const chevron = head.querySelector('.mod-group-chevron');
        if (!searchQ) {
          head.style.display = '';
          body.style.display = collapsedOwnerGroups.has(owner) ? 'none' : '';
          if (chevron) chevron.textContent = collapsedOwnerGroups.has(owner) ? '▸' : '▾';
          body.querySelectorAll('.mod-item').forEach(item => { item.style.display = ''; });
          return;
        }
        let anyMatch = false;
        body.querySelectorAll('.mod-item').forEach(item => {
          const mk = (item.dataset.mod || '').toLowerCase();
          const name = (item.querySelector('.mod-name')?.textContent || '').toLowerCase();
          const match = mk.includes(searchQ) || name.includes(searchQ);
          item.style.display = match ? '' : 'none';
          if (match) anyMatch = true;
        });
        head.style.display = anyMatch ? '' : 'none';
        body.style.display = anyMatch ? '' : 'none';
        if (anyMatch && chevron) chevron.textContent = '▾';
      });
    }

    // ── PROJECT / CONTRACT ARTIFACT VIEWS ─────────────────────
    function projectFact(label, value) {
      return `<div class="project-fact"><div class="project-fact-label">${escapeHtml(label)}</div><div class="project-fact-value">${escapeHtml(value ?? '—')}</div></div>`;
    }

    function renderProject() {
      const identity = projectData.identity || {};
      const workflow = projectData.workflow || {};
      const generation = artifactManifest?.generation_id ? artifactManifest.generation_id.slice(0,12) : 'legacy projection';
      artifactStatus.textContent = artifactMode === 'canonical'
        ? `Canonical generation ${generation} · ${artifactManifest.generated_at}`
        : 'Legacy compatibility projection — run ai-kit visualizer serve for manifest-first loading.';
      projectOverview.innerHTML = [
        projectFact('Project', identity.name || 'Unnamed project'),
        projectFact('Architecture', identity.architecture || 'Not declared'),
        projectFact('Stack', (projectData.stack || []).join(', ') || '—'),
        projectFact('Workflow', workflow.title ? `${workflow.title} · r${workflow.revision}` : 'No active workflow'),
        projectFact('Git branch', gitData.branch || (gitData.repository === false ? 'Not a Git repository' : 'Detached / unavailable')),
        projectFact('Git HEAD', gitData.head ? gitData.head.slice(0,12) : '—'),
        projectFact('Working tree', gitData.dirty ? `${(gitData.tracked_changes||[]).length} tracked · ${(gitData.untracked_paths||[]).length} untracked` : 'Clean'),
        projectFact('Ownership', `${Object.keys(ownershipData.owners||{}).length} owners · ${(ownershipData.unowned||[]).length} unowned`),
        projectFact('Evidence', `${evidenceData.counts?.current || 0} current · ${evidenceData.counts?.stale || 0} stale`),
      ].join('');
      const risks = risksData.items || [];
      projectRisks.innerHTML = risks.length ? risks.slice(0,20).map(risk=>`
        <div class="risk-row"><div class="risk-kind">${escapeHtml(risk.kind)}</div><div class="risk-detail">${escapeHtml(risk.detail)}</div></div>
      `).join('') : '<div class="view-sub">No open projected risks.</div>';
    }

    function renderContracts() {
      const contracts = contractsData.items || [];
      const edges = contractsData.edges || [];
      const statusCounts = contracts.reduce((acc,item)=>{ acc[item.status]=(acc[item.status]||0)+1; return acc; },{});
      contractSummary.innerHTML = [
        projectFact('Versions', contracts.length),
        projectFact('Active', statusCounts.active || 0),
        projectFact('Approved', statusCounts.approved || 0),
        projectFact('Relations', edges.length),
      ].join('');
      const taskLabels = Object.fromEntries(taskItemsData.map(task=>[task.id, `${task.task_id} · ${task.owner}`]));
      const lifecycle = ['draft','proposed','approved','active','deprecated','removed'];
      contractGraph.innerHTML = contracts.map(contract=>{
        const incoming = edges.filter(edge=>edge.to===contract.id && edge.relation!=='represents');
        const relations = incoming.length ? incoming.map(edge=>`
          <div class="contract-relation">
            <span>${escapeHtml(taskLabels[edge.from] || edge.from)}</span>
            <span class="contract-relation-arrow">—${escapeHtml(edge.relation)}→</span>
            <span class="contract-relation-label">${escapeHtml(contract.contract_id)}@${escapeHtml(contract.version)}</span>
          </div>`).join('') : '<div class="view-sub" style="margin:0">No producer/consumer/verifier task relation.</div>';
        return `<div class="contract-graph-row">
          <div class="contract-domain">represents → ${escapeHtml(contract.represents || 'unassigned domain')}</div>
          <div class="contract-node">
            <div class="contract-node-name">${escapeHtml(contract.contract_id)}@${escapeHtml(contract.version)}</div>
            <div class="contract-lifecycle">${lifecycle.map(status=>`<span class="${status===contract.status?'current':''}">${status}</span>`).join('<span>→</span>')}</div>
          </div>
          <div class="contract-relations">${relations}</div>
        </div>`;
      }).join('');
      if (!contracts.length) contractGraph.innerHTML = '<div class="view-sub">No registered contract graph.</div>';
      contractBody.innerHTML = contracts.map(contract=>{
        const relations = edges.filter(edge=>edge.to===contract.id && edge.relation!=='represents').map(edge=>edge.relation);
        return `<tr>
          <td><strong>${escapeHtml(contract.contract_id)}@${escapeHtml(contract.version)}</strong></td>
          <td>${escapeHtml(contract.kind)}</td><td>${escapeHtml(contract.owner || '—')}</td>
          <td><span class="badge bg-cyan">${escapeHtml(contract.status)}</span></td>
          <td>${escapeHtml(contract.represents || '—')}</td><td>${escapeHtml(relations.join(', ') || '—')}</td>
        </tr>`;
      }).join('');
      if (!contracts.length) contractBody.innerHTML = '<tr><td colspan="6" style="color:var(--text-dim)">No registered contracts.</td></tr>';
    }

    // ── RENDER STATS ──────────────────────────────────────────
    function renderStats() {
      const total = allTasks().length;
      const done = (board.done||[]).length;
      const mods = allMods().length;
      statsRow.innerHTML = `
        <div class="stat-card"><div class="stat-n">${total}</div><div class="stat-l">Tasks</div></div>
        <div class="stat-card"><div class="stat-n" style="color:var(--green)">${done}</div><div class="stat-l">Done</div></div>
        <div class="stat-card"><div class="stat-n">${mods}</div><div class="stat-l">Modules</div></div>
      `;
    }

    // ── RENDER AGENTS (from events) ───────────────────────────
    function renderAgents() {
      const actorMap = {};
      events.forEach(e => {
        actorMap[e.actor] = { lastAction: e.action, lastTask: e.task, ts: e.ts };
      });
      agentList.innerHTML = '';
      Object.entries(actorMap).forEach(([actor, info]) => {
        const row = document.createElement('div');
        row.className = 'agent-row';
        row.innerHTML = `
          <div class="agent-av">${actor.slice(0,2).toUpperCase()}</div>
          <div style="flex:1;min-width:0;">
            <div class="agent-name">@${actor}</div>
            <div class="agent-task">${info.lastAction} ${info.lastTask||''}</div>
          </div>
          <div class="pulse"></div>
        `;
        agentList.appendChild(row);
      });
    }

    // ── RENDER EVENTS ─────────────────────────────────────────
    function renderEvents() {
      const last = events.slice(-12).reverse();
      evCount.textContent = events.length;
      eventList.innerHTML = '';
      last.forEach((e, i) => {
        const item = document.createElement('div');
        const cls = e.action==='complete'||e.action==='close' ? 'ev-done'
                  : e.action==='reject' ? 'ev-reject'
                  : e.action.includes('qa') ? 'ev-qa'
                  : i===0 ? 'ev-latest' : '';
        item.className = `ev-item ${cls}`;
        item.innerHTML = `
          <span class="ev-actor">@${e.actor}</span>
          <span class="ev-action"> → ${e.action}</span>
          ${e.task ? `<span class="ev-task"> ${e.task}</span>` : ''}
          <div class="ev-time">${formatTs(e.ts)} · ${timeAgo(e.ts)}</div>
        `;
        eventList.appendChild(item);
      });
    }

    // ── RENDER EVOLUTION BOARD ────────────────────────────────
    function renderEvolution() {
      const cols = [
        { key:'todo', label:'To Do', cls:'col-todo', color:'var(--text-dim)' },
        { key:'in-progress', label:'In Progress', cls:'col-ip', color:'var(--purple)' },
        { key:'implementation-complete', label:'Complete', cls:'col-ic', color:'var(--cyan)' },
        { key:'qa-passed', label:'QA Passed', cls:'col-qa', color:'var(--amber)' },
        { key:'done', label:'Done', cls:'col-done', color:'var(--green)' },
      ];
      evoBoard.innerHTML = '';
      cols.forEach(col => {
        const tasks = board[col.key] || [];
        if (!tasks.length && col.key!=='todo') return;
        const div = document.createElement('div');
        div.className = `evo-col ${col.cls}`;
        div.innerHTML = `<div class="evo-col-head">
          <span style="color:${col.color}">${col.label}</span>
          <span style="font-family:var(--mono)">${tasks.length}</span>
        </div>`;
        tasks.forEach(t => {
          const card = document.createElement('div');
          card.className = 'evo-card';
          const tags = (t.tags||[]).map(tg=>`<span class="tag-chip">${tg}</span>`).join('');
          card.innerHTML = `
            <div class="task-id">${t.id}</div>
            <div class="task-title">${t.title}</div>
            <div class="task-owner">@${t.owner_display} · ${t.acceptance_count||0} criteria</div>
            <div style="margin-top:4px;">${tags}</div>
          `;
          div.appendChild(card);
        });
        evoBoard.appendChild(div);
      });
    }

    // ── RENDER RUNTIME TABLE ─────────────────────────────────
    function renderRuntime() {
      const assigned = taskItemsData.filter(task=>task.assignment);
      if (assigned.length) {
        runtimeBody.innerHTML = assigned.map(task=>`<tr>
          <td><strong>@${escapeHtml(task.assignment.agent_id || task.owner)}</strong></td>
          <td>${escapeHtml(task.assignment.runner || 'assigned')}</td>
          <td><span style="font-family:var(--mono); color:var(--cyan);">${escapeHtml(task.task_id)}</span></td>
          <td style="font-family:var(--mono); font-size:10px;">${escapeHtml(task.assignment.assigned_at || '—')}</td>
          <td><span class="badge bg-cyan">${escapeHtml(task.status)} · ${task.evidence_refs.length} evidence</span></td>
        </tr>`).join('');
        return;
      }
      const actorMap = {};
      events.forEach(e => {
        actorMap[e.actor] = { action:e.action, task:e.task, ts:e.ts };
      });
      runtimeBody.innerHTML = '';
      Object.entries(actorMap).forEach(([actor,info]) => {
        const statusCls = info.action==='complete'||info.action==='close' ? 'bg-green'
                        : info.action==='reject' ? 'bg-amber' : 'bg-cyan';
        runtimeBody.innerHTML += `
          <tr>
            <td><strong>@${actor}</strong></td>
            <td>${info.action}</td>
            <td><span style="font-family:var(--mono); color:var(--cyan);">${info.task||'—'}</span></td>
            <td style="font-family:var(--mono); font-size:10px;">${formatTs(info.ts)}</td>
            <td><span class="badge ${statusCls}">${info.action}</span></td>
          </tr>
        `;
      });
    }

    // ── RENDER REPLAY TABLE ───────────────────────────────────
    function renderReplay() {
      replayBody.innerHTML = '';
      events.forEach((e, i) => {
        replayBody.innerHTML += `
          <tr style="cursor:pointer;" onclick="replayTo(${i})">
            <td style="font-family:var(--mono); color:var(--text-dim);">${i+1}</td>
            <td style="font-family:var(--mono); font-size:10px;">${formatTs(e.ts)}</td>
            <td>@${e.actor}</td>
            <td><span class="badge ${e.action.includes('complete')||e.action==='close'?'bg-green':e.action==='reject'?'bg-amber':'bg-cyan'}">${e.action}</span></td>
            <td style="font-family:var(--mono); color:var(--cyan);">${e.task||'—'}</td>
            <td style="color:var(--text-dim); font-size:10px; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${e.detail||''}</td>
          </tr>
        `;
      });
    }
    window.replayTo = function(idx) {
      const event = events[idx];
      replayTimestamp = event ? Date.parse(event.ts) : null;
      renderAll();
    };

    // ── RENDER CANVAS ─────────────────────────────────────────
    function renderCanvas() {
      const pos = getPositions();
      const mods = allMods().filter(moduleVisibleAtReplay);

      // Clear
      nodesLayer.innerHTML = '';
      archSvg.innerHTML = '';
      nodesLayer.classList.toggle('labels-off', !layers.labels);

      // Draw edges (arch dependency)
      if (layers.arch) {
        architectureEdges.forEach(edge => {
            if (!mods.includes(edge.from) || !mods.includes(edge.to) || !pos[edge.from] || !pos[edge.to]) return;
            const x1 = pos[edge.from].x + 93, y1 = pos[edge.from].y + 20;
            const x2 = pos[edge.to].x + 93, y2 = pos[edge.to].y + 80;
            const mx = (x1+x2)/2;
            const path = document.createElementNS('http://www.w3.org/2000/svg','path');
            path.setAttribute('d', `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`);
            const classification = edge.observation?.classification || 'observed';
            path.setAttribute('class', `edge-path edge-dep edge-${classification}`);
            path.setAttribute('data-observation', classification);
            archSvg.appendChild(path);
        });
      }

      // Draw nodes
      mods.forEach(k => {
        const m = arch[k];
        if (!m || !pos[k]) return;
        const p = pos[k];
        const ownerC = ownerColor(m.owner);
        const deps = m.depends_on || [];
        const impData = impact[k] || {};
        const impCount = (impData.all_dependents||[]).length;

        const card = document.createElement('div');
        const observationClass = m.observation?.classification || 'observed';
        card.className = 'node-card' + (selectedMod===k?' selected':'') + ` node-observation-${observationClass}`;
        if (isReplayMode() && moduleTasks(k).length && !replaySeenModules.has(k)) card.classList.add('node-replay-in');
        card.dataset.mod = k;
        card.style.left = p.x+'px';
        card.style.top = p.y+'px';

        // Owner border accent
        if (layers.owner) {
          card.style.borderLeftColor = ownerC;
          card.style.borderLeftWidth = '3px';
        }

        const shortName = k.replace(/^(backend|extension)-/,'');
        const ownerChipCls = m.owner==='backend'?'s-be':m.owner==='frontend'?'s-fe':'s-qa';

        const stats = moduleStats(k);
        card.innerHTML = `
          <div class="node-top">
            <div class="node-icon" style="background:${ownerC}18; color:${ownerC};">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                ${m.owner==='frontend'
                  ? '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/>'
                  : '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/>'}
              </svg>
            </div>
            <span class="chip-status ${ownerChipCls}">${m.owner||'unowned'}</span>
            <span class="mod-src-tag ${m.source==='discovered'?'mod-src-discovered':'mod-src-declared'}" style="margin-left:auto;">${m.source||'declared'}${m.source==='discovered' && m.confidence!=null ? ' ' + Math.round(m.confidence*100) + '%' : ''}</span>
          </div>
          <div class="node-name">${shortName}</div>
          <div class="obs-label obs-${observationClass}">${observationClass}</div>
          <div class="node-path">${m.parent ? '↳ ' + m.parent + ' / ' : ''}${m.path}</div>
          ${layers.evolution ? `<div class="prog-row"><span>acceptance</span><span>${stats.passed}/${stats.total}</span></div>
          <div class="prog-bar"><div class="prog-fill" style="width:${Math.round(stats.ratio * 100)}%;"></div></div>` : ''}
          ${layers.rev ? `<div class="prog-row"><span>rev</span><span>${m.revision||1}</span></div>
          <div class="prog-bar"><div class="prog-fill" style="width:${Math.min(100,(m.revision||1)*33)}%;"></div></div>` : ''}
          ${deps.length ? `<div class="node-deps">${deps.map(d=>`<span class="dep-pill">${d.replace(/^(backend|extension)-/,'')}</span>`).join('')}</div>` : ''}
          ${impCount > 0 ? `<div style="margin-top:5px;font-size:9px;color:var(--amber);">↑ ${impCount} dependents</div>` : ''}
        `;

        card.addEventListener('click', () => selectModule(k));
        nodesLayer.appendChild(card);
      });

      if (isReplayMode()) mods.forEach(k => replaySeenModules.add(k));
      applySearch();
      updateMinimap();
    }

    // ── SELECT MODULE ─────────────────────────────────────────
    function selectModule(k) {
      selectedMod = k;

      // Highlight nodes
      document.querySelectorAll('.node-card').forEach(card => {
        card.classList.remove('selected','impact-hi','impact-dep','dimmed');
      });

      const impData = impact[k] || {};
      const allDeps = impData.all_dependents || [];
      const myDeps = arch[k]?.depends_on || [];

      document.querySelectorAll('.node-card').forEach(card => {
        const mk = card.dataset.mod;
        if (mk === k) { card.classList.add('selected'); }
        else if (layers.impact && allDeps.includes(mk)) { card.classList.add('impact-hi'); }
        else if (layers.impact && myDeps.includes(mk)) { card.classList.add('impact-dep'); }
      });

      // Update mod tree
      document.querySelectorAll('.mod-item').forEach(i => {
        i.classList.toggle('active', i.dataset.mod===k);
      });

      // Update sel card in inspector right
      const m = arch[k] || {};
      const ownerC = ownerColor(m.owner);
      selCard.innerHTML = `
        <div class="sel-card">
          <div class="sel-head">
            <div class="sel-icon" style="border-color:${ownerC};">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="${ownerC}" stroke-width="2">
                <polygon points="12 2 2 7 12 12 22 7 12 2"/>
              </svg>
            </div>
            <div>
              <div class="sel-name">${k}</div>
              <div class="sel-path">${m.path||''}</div>
            </div>
          </div>
          <div class="sel-row"><span>Owner</span><span class="badge" style="background:${ownerC}18;color:${ownerC};">@${m.owner||'—'}</span></div>
          <div class="sel-row"><span>Revision</span><span class="badge bg-cyan">r${m.revision||1}</span></div>
          <div class="sel-row"><span>Depends on</span><span>${(m.depends_on||[]).length} modules</span></div>
          <div class="impact-info">
            <div style="color:var(--text-dim); margin-bottom:3px;">Impact (all dependents)</div>
            <div class="impact-num">${(impact[k]?.all_dependents||[]).length} modules</div>
          </div>
        </div>
      `;

      if (layers.impact) currentTab = 'impact';
      renderInspector();
    }

    // ── RENDER INSPECTOR ──────────────────────────────────────
    function renderInspector() {
      if (currentTab === 'risks') {
        const risks = risksData.items || [];
        inspBody.innerHTML = `<div class="ib ib-full"><div class="ib-k">Projected risks</div>${risks.length ? risks.map(risk=>`
          <div class="risk-row"><div class="risk-kind">${escapeHtml(risk.kind)}</div><div class="risk-detail">${escapeHtml(risk.detail)}</div></div>
        `).join('') : '<div class="ib-v">No projected risks.</div>'}</div>`;
        return;
      }
      if (currentTab === 'evidence') {
        const items = evidenceData.items || [];
        inspBody.innerHTML = `<div class="ib ib-full"><div class="ib-k">Evidence index</div>${items.length ? items.map(item=>`
          <div class="risk-row" style="border-left-color:${item.current?'var(--green)':'var(--amber)'}">
            <div class="risk-kind" style="color:${item.current?'var(--green)':'var(--amber)'}">${escapeHtml(item.id)} · ${item.current?'current':'stale'}</div>
            <div class="risk-detail">${escapeHtml(item.verdict ?? 'no verdict')} · ${escapeHtml(item.path)}</div>
          </div>`).join('') : '<div class="ib-v">No evidence artifacts.</div>'}</div>`;
        return;
      }
      if (!selectedMod) {
        inspBody.innerHTML = '<div class="ib ib-full" style="color:var(--text-dim)">← Click a module node to inspect details</div>';
        return;
      }
      const k = selectedMod;
      const m = arch[k] || {};
      const impData = impact[k] || {};

      if (currentTab === 'info') {
        const moduleWarnings = discoveryWarnings.filter(w => (w.detail||'').includes(`'${k}'`));
        inspBody.innerHTML = `
          <div class="ib"><div class="ib-k">Module</div><div class="ib-v">${k}</div></div>
          <div class="ib"><div class="ib-k">Owner</div><div class="ib-v" style="color:${ownerColor(m.owner)}">@${m.owner||'—'}</div></div>
          <div class="ib"><div class="ib-k">Source</div><div class="ib-v"><span class="mod-src-tag ${m.source==='discovered'?'mod-src-discovered':'mod-src-declared'}">${m.source||'declared'}</span></div></div>
          <div class="ib"><div class="ib-k">Observation</div><div class="ib-v obs-${m.observation?.classification||'observed'}">${m.observation?.classification||'observed'} · ${Math.round((m.observation?.confidence??1)*100)}%</div></div>
          <div class="ib"><div class="ib-k">Kind</div><div class="ib-v">${m.kind||'bounded-context'}</div></div>
          ${m.source==='discovered' ? `<div class="ib"><div class="ib-k">Confidence</div><div class="ib-v">${Math.round((m.confidence||0)*100)}%${m.framework?` (${m.framework})`:''}</div></div>` : ''}
          ${m.parent ? `<div class="ib"><div class="ib-k">Parent context</div><div class="ib-v">${m.parent}</div></div>` : ''}
          <div class="ib"><div class="ib-k">Revision</div><div class="ib-v">r${m.revision||1}</div></div>
          <div class="ib"><div class="ib-k">Path</div><div class="ib-v" style="font-family:var(--mono);font-size:10px;">${m.path||'—'}</div></div>
          <div class="ib"><div class="ib-k">Depends On</div><div class="ib-v">${(m.depends_on||[]).join(', ')||'None'}</div></div>
          <div class="ib"><div class="ib-k">All Dependents</div><div class="ib-v" style="color:var(--amber)">${(impData.all_dependents||[]).join(', ')||'None'}</div></div>
          ${moduleWarnings.length ? `<div class="ib ib-full"><div class="ib-k" style="color:var(--amber);">Warnings</div>${moduleWarnings.map(w=>`<div style="font-size:10px;color:var(--amber);margin-top:4px;">⚠ ${w.kind}: ${w.detail}</div>`).join('')}</div>` : ''}
        `;
      } else if (currentTab === 'deps') {
        const deps = m.depends_on||[];
        const rev = impData.direct_dependents||[];
        inspBody.innerHTML = `
          <div class="ib ib-full">
            <div class="ib-k">Requires (depends_on)</div>
            <div class="pill-row" style="margin-top:6px;">${deps.map(d=>`<span class="pill pill-c">${d}</span>`).join('')||'<span style="color:var(--text-dim);font-size:11px;">None</span>'}</div>
          </div>
          <div class="ib ib-full">
            <div class="ib-k">Direct Dependents (consumers)</div>
            <div class="pill-row" style="margin-top:6px;">${rev.map(d=>`<span class="pill pill-p">${d}</span>`).join('')||'<span style="color:var(--text-dim);font-size:11px;">None (leaf node)</span>'}</div>
          </div>
          <div class="ib ib-full">
            <div class="ib-k">All transitive dependents</div>
            <div class="pill-row" style="margin-top:6px;">${(impData.all_dependents||[]).map(d=>`<span class="pill" style="background:rgba(245,158,11,.1);color:var(--amber);">${d}</span>`).join('')||'<span style="color:var(--text-dim);font-size:11px;">None</span>'}</div>
          </div>
        `;
      } else if (currentTab === 'impact') {
        const allDeps = impData.all_dependents||[];
        inspBody.innerHTML = `
          <div class="ib"><div class="ib-k">Impact Score</div><div class="ib-v" style="font-size:22px;color:var(--amber);font-family:var(--mono);">${allDeps.length}</div></div>
          <div class="ib"><div class="ib-k">Direct</div><div class="ib-v">${(impData.direct_dependents||[]).length}</div></div>
          <div class="ib"><div class="ib-k">Transitive</div><div class="ib-v">${allDeps.length - (impData.direct_dependents||[]).length}</div></div>
          <div class="ib ib-full">
            <div class="ib-k">Cascade chain</div>
            <div style="margin-top:6px; display:flex; flex-direction:column; gap:4px;">
              ${allDeps.map(d=>`<div style="font-size:10px; color:var(--text-sub); display:flex; align-items:center; gap:6px;"><span style="color:var(--amber);">↑</span>${d}</div>`).join('')||'<span style="color:var(--text-dim);">No downstream impact</span>'}
            </div>
          </div>
        `;
      } else if (currentTab === 'tasks') {
        const relTasks = moduleTasks(k);
        inspBody.innerHTML = `
          <div class="ib ib-full">
            <div class="ib-k">Related Tasks (by context, then file path)</div>
            ${relTasks.length ? relTasks.map(t=>`
              <div style="margin-top:8px; background:rgba(255,255,255,.03); border:1px solid var(--border); border-radius:7px; padding:8px;">
                <div style="font-family:var(--mono); color:var(--cyan); font-size:10px;">${t.id}</div>
                <div style="font-size:11px; color:var(--text-sub); margin-top:3px;">${t.title}</div>
                <div style="font-size:10px; color:var(--text-dim); margin-top:3px;">@${t.owner_display} · ${t.acceptance_count||0} criteria</div>
              </div>
            `).join('') : '<div style="color:var(--text-dim); font-size:11px; margin-top:8px;">No tasks directly reference this module</div>'}
          </div>
        `;
      } else if (currentTab === 'events') {
        const modShort = k.split('-').slice(1).join('-');
        const relEvs = events.filter(e => e.detail?.includes(k) || e.detail?.includes(modShort) || e.task?.includes(k)).slice(-8);
        inspBody.innerHTML = `
          <div class="ib ib-full">
            <div class="ib-k">Module Events (from events.json)</div>
            ${relEvs.length ? relEvs.map(e=>`
              <div style="margin-top:6px; border-left:2px solid var(--cyan); padding-left:8px;">
                <span style="color:var(--text-sub); font-size:11px;">@${e.actor} → <strong>${e.action}</strong></span>
                ${e.task ? `<span style="font-family:var(--mono); color:var(--cyan); font-size:10px;"> ${e.task}</span>` : ''}
                <div style="font-size:9px; color:var(--text-dim);">${formatTs(e.ts)}</div>
              </div>
            `).join('') : '<div style="color:var(--text-dim); font-size:11px; margin-top:8px;">No events mention this module directly</div>'}
          </div>
        `;
      }
    }

    // ── TIMELINE ─────────────────────────────────────────────
    timelineSlider.addEventListener('input', e => {
      const bounds = replayBounds();
      if (!bounds) return;
      replayTimestamp = Number(e.target.value) >= bounds.max ? null : Number(e.target.value);
      renderAll();
    });
    btnPlay.addEventListener('click', () => {
      isPlaying = !isPlaying;
      if (isPlaying) {
        playIcon.innerHTML = `<rect x="4" y="3" width="4" height="18"/><rect x="14" y="3" width="4" height="18"/>`;
        playInterval = setInterval(() => {
          const bounds = replayBounds();
          if (!bounds) return;
          const current = replayTimestamp === null ? bounds.min : replayTimestamp;
          const next = Math.min(bounds.max, current + Math.max(1000, Math.round((bounds.max - bounds.min) / 100)));
          replayTimestamp = next >= bounds.max ? null : next;
          renderAll();
          if (replayTimestamp === null) {
            isPlaying = false;
            clearInterval(playInterval);
            playIcon.innerHTML = `<polygon points="5 3 19 12 5 21 5 3"/>`;
          }
        }, 100);
      } else {
        playIcon.innerHTML = `<polygon points="5 3 19 12 5 21 5 3"/>`;
        clearInterval(playInterval);
      }
    });

    function updateTimecode() {
      const bounds = replayBounds();
      if (!bounds) {
        tcLabel.textContent = 'No events';
        return;
      }
      const current = replayTimestamp === null ? bounds.max : replayTimestamp;
      const visible = events.filter(event => Date.parse(event.ts) <= current).length;
      tcLabel.textContent = `${visible} / ${events.length} events`;
      timelineSlider.min = String(bounds.min);
      timelineSlider.max = String(bounds.max);
      timelineSlider.value = String(current);
    }

    // ── FULL RENDER ───────────────────────────────────────────
    function renderAll() {
      renderProject();
      renderContracts();
      refreshFilterOptions();
      renderModTree();
      renderStats();
      renderAgents();
      renderEvents();
      renderEvolution();
      renderRuntime();
      renderReplay();
      renderCanvas();
      renderInspector();
      updateTimecode();
    }

    renderAll();

  })();
