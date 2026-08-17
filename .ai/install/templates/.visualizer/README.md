# AI-Kit Visualizer

Visualizer là consumer chỉ đọc của Artifact Architecture Machine. Dashboard
không scan source, suy luận dependency, tính lifecycle, hoặc ghi state. Nó chỉ
lọc, nhóm và bố trí dữ liệu đã được `artifact generate` publish.

## Chạy

```bash
python3 .ai/engine/ai_kit.py artifact generate
python3 .ai/engine/ai_kit.py artifact validate
python3 .ai/engine/ai_kit.py visualizer serve --host 127.0.0.1 --port 8080
```

Mở `http://127.0.0.1:8080/index.html`. `visualizer serve` chọn đúng workspace
của `--state`, phục vụ static assets và endpoint chỉ đọc
`/artifacts/project/*`. `visualizer generate` vẫn tồn tại như alias deprecated
và chỉ delegate sang `artifact generate`.

## Nguồn dữ liệu canonical

Dashboard đọc `.ai-work/artifacts/project/manifest.json` trước, sau đó tải song
song đủ 12 payload: `project`, `architecture`, `modules`, `dependencies`,
`contracts`, `tasks`, `dag`, `ownership`, `risks`, `git`, `evidence`, và
`events`. Nó kiểm schema và `generation_id`, retry một lần nếu publication đang
diễn ra, và giữ render trước nếu generation mới chưa hoàn chỉnh.

Các tab có ý nghĩa:

- **Project**: identity, stack, freshness, Git, ownership, risk, evidence.
- **Architecture**: contexts, modules, dependency/ownership graph và provenance.
- **Architecture C4**: chuyển giữa System Context, Containers, Components và
  module graph từ trường `architecture.json.data.c4` canonical.
- **Contracts**: lifecycle và impact graph canonical tới operation,
  event/message, schema, field và generated output. Bộ lọc entity/relation,
  node inspector và deep-link `#view=contracts&contract=...&entity=...` chỉ
  chiếu dữ liệu đã publish; chúng không tự suy luận contract relationship.
- **Evolution**: board projection từ `tasks.json`.
- **Runtime**: assignment và gate/evidence state.
- **Replay**: cửa sổ tối đa 200 lifecycle event từ `events.json`.
- **DAG**: waves, ready set, critical path và dependency unlock state.

`dag-view.js` là renderer DAG dùng chung. Dashboard chuyển `dag.json` đã tải
từ canonical bundle vào renderer, nên chuyển tab hay refresh không tạo thêm
một loader độc lập. `dag.html` chỉ là compatibility shell (canonical-first,
legacy fallback) và cũng gọi cùng renderer. Deep-link dùng
`#view=dag&task=T1`; selection của DAG đồng bộ với inspector của dashboard.

Observation luôn có label và line style riêng: `observed` nét liền,
`inferred` nét đứt, `proposed` nét chấm. Proposed edge chỉ hiển thị; nó không
tham gia impact, ownership, QA hay dependency gating.

## Publication và compatibility

`manifest.json` là atomic commit marker và được ghi cuối. Artifact bundle là
derived canonical projection, không phải lifecycle authority. Audit history
gốc vẫn nằm ở `.ai-work/logs/events.jsonl`; `events.json` chỉ phục vụ replay.

Trong phase tương thích này, generator tạo `board.json`, `impact.json`,
`architecture.json`, `contracts.json`, `events.json`, `dag.json`,
`discovered-architecture.json`, và `artifacts.json` trong `.visualizer/` từ
chính bundle vừa publish. Browser dùng chúng làm fallback cho static server cũ;
không payload legacy nào được tính độc lập.
