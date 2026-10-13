# The Void in diagrams

Flowcharts of how the system works, drawn from the code. They render on GitHub and in Obsidian (Mermaid). PNG renders of each are in [diagrams/](diagrams/), regenerated with `npx @mermaid-js/mermaid-cli` from the blocks below. For the full specification see [DESIGN.md](DESIGN.md); for operations, [RUNBOOK.md](RUNBOOK.md); for the research plan, [research/MEMORY_EVOLUTION.md](research/MEMORY_EVOLUTION.md).

## 1. The whole system

```mermaid
flowchart LR
    subgraph Brains["Brains: one model call per agent per tick"]
        A["Anthropic tiers"]
        G["Gemini tiers"]
        S["Scripted brains: free, deterministic"]
    end
    subgraph Sim["Simulation loop: void/sim"]
        L["Tick loop"]
        O["Observation builder"]
        D["Day boundary: chronicle, maintenance, exams"]
    end
    subgraph Rules["Kernel: rules agents cannot touch"]
        K["World kernel: nodes, weather, actions, claims"]
        W["Wallet and ledger: real-dollar caps"]
        LC["Lifecycle: birth, bankruptcy, inheritance"]
        SB["Gadget sandbox and verification gate"]
    end
    subgraph Mem["Memory"]
        V["Markdown vaults with wikilinks"]
        IX["SQLite index, manifest, retrieval"]
        P["Memory policy file"]
    end
    subgraph Cul["Culture"]
        GS["Gossip with provenance"]
        CH["Chronicle"]
        GY["Graveyard"]
    end
    subgraph Res["Research harness"]
        M["Metrics per tick and day"]
        EX["Audit exams"]
        C["compare.py, analyze.py, export"]
    end
    DB[("world.db")]
    SRV["Server: WebSocket feed and REST"]
    WEB["Web: 3D world and control room"]

    O --> L
    L -->|prompt| Brains
    Brains -->|decision JSON| L
    L --> K & W & LC & SB
    L --> V
    V <--> IX
    IX --> O
    P --> O
    L --> GS --> V
    D --> CH & P & EX
    LC --> GY
    K & W & LC & IX & M --> DB
    DB --> C
    L --> M
    DB --> SRV --> WEB
```

## 2. One tick

```mermaid
flowchart TD
    T0["Advance the clock; mark tick in progress"] --> NEW{"New day?"}
    NEW -->|yes| DAY["Day boundary: see diagram 3"]
    NEW -->|no| ENV
    DAY --> ENV["Epochs, benefactor grants, weather step, node regrowth"]
    ENV --> ORDER["Awake agents in a seeded random order"]
    ORDER --> OBS["Per agent: entropy step, retrieve memories, build observation"]
    OBS --> GATE{"Wallet gate: can it afford the worst-case call?"}
    GATE -->|no| BLOCK["Gate blocked: sleep, or pending bankruptcy"]
    GATE -->|yes| CALL["Decide concurrently: one model call per agent"]
    CALL --> METER["Meter each call at its real cost; release the hold"]
    METER --> TX["Tick transaction"]
    subgraph TX_["Inside one database transaction, per agent"]
        TX --> CORR["Degeneration: corrupt the decision if over the entropy threshold"]
        CORR --> MEAS["Measure: coherence, claims checked against the world"]
        MEAS --> MEMOPS["Memory ops: remember, revise_self"]
        MEMOPS --> APPLY["Kernel applies the action: move, forage, talk, transfer, gadget, sleep"]
    end
    APPLY --> GOSSIP["Gossip flush: paid paraphrases, outside the transaction"]
    GOSSIP --> LIFE["Lifecycle: births, bankruptcies, deaths"]
    LIFE --> MET["Metrics row; clear tick marker; archive vaults of the dead"]
    MET --> FEED["Events to the bus, the WebSocket feed and the 3D client"]
```

## 3. The day boundary

```mermaid
flowchart TD
    A["Midnight"] --> B["Chronicle writes up yesterday from the event log"]
    B --> C["Reset daily spend and caps; wake sleepers"]
    C --> D["Nightly maintenance, every living agent"]
    subgraph Maint["One paid structured call per agent"]
        D --> D1{"Wallet gate"}
        D1 -->|cannot afford| D2["Skipped and logged: poverty degrades memory"]
        D1 -->|ok| D3["Prompt: policy, vault listing, today's notes, outcomes"]
        D3 --> D4["Model or planted strategy returns a batch of memory commands"]
        D4 --> D5["Kernel validates and applies: create, str_replace, insert, delete, rename"]
        D5 --> D6["Policy edits become new versions with their source"]
    end
    D6 --> E["Audit exam: questions from what each agent was shown days ago"]
    E --> E1["Graded by exact match; paid by the research pool, never by the agent"]
    E1 --> F["Day report rides on the metrics row"]
```

## 4. Money

Every model call is paid from a wallet; the same mechanism is the fictional "no money, you die" rule and the real-dollar safety cutoff.

```mermaid
flowchart LR
    WORLD(["World"]) -->|forage income| AG
    BEN(["Benefactor"]) -->|windfall, task reward| AG
    subgraph AG["Agent wallets"]
        A1["Agent"]
        A2["Agent"]
    end
    A1 <-->|transfers| A2
    AG -->|"model calls: decide, maintain"| API(["Model provider: real dollars"])
    AG -->|gadget and pitch fees| HOUSE["house"]
    SPAWN["spawn_pool"] -->|starting balance| AG
    AG -->|estate on death| SPAWN
    CHRON["chronicle wallet"] -->|chronicle calls| API
    RP["research_pool"] -->|exam calls| API
    CAPS{{"Caps: daily and total, enforced by a hold before every call"}} -.-> API
```

## 5. An agent's life

```mermaid
stateDiagram-v2
    [*] --> Born: founder, or child of a parent
    Born --> Awake
    Awake --> Asleep: chooses sleep, or daily cap reached
    Asleep --> Awake: next day
    Awake --> Degenerating: entropy budget spent
    Degenerating --> Awake: budget recovers
    Awake --> Reproducing: enough balance above the reserve
    Reproducing --> Awake: child born with inherited notes, policy and personality
    Awake --> Bankrupt: cannot afford its next call
    Bankrupt --> Archived: estate to spawn pool, vault to graveyard
    Archived --> [*]
```

## 6. Memory

```mermaid
flowchart TD
    OBS["What the agent sees: fog limits it to a view radius"] --> REM["remember: a markdown note with tags and links"]
    REM --> VAULT["Vault: notes, index notes, self.md, memory_policy.md"]
    VAULT --> IDX["Kernel index in SQLite: provenance, hash manifest, links"]
    IDX --> RET["Retrieval: entry points by similarity, 2-hop link walk, recency, top k"]
    RET --> PROMPT["Prompt: memories, self summary, policy written by the agent"]
    PROMPT --> DEC["Decision"]
    DEC --> REM
    GOS["Gossip from a neighbour: a copied note, slightly paraphrased"] --> VAULT
    INH["Inheritance at birth: top notes and the parent's policy"] --> VAULT
    MAINT["Nightly maintenance: six memory commands"] --> VAULT
    FEED["Claim feedback: told when a claim it heard was false"] --> OBS
    EXAM["Audit exam: was it remembered right?"] -. reads .-> IDX
```

## 7. Behaviour in the 3D client

```mermaid
flowchart LR
    EV["Server event: talk, remember, claim_feedback, maintenance, forage..."] --> Q["Effects queue keyed by tick"]
    Q -->|when the render clock reaches the tick| FX["Fire effects"]
    FX --> BUB["Bubbles: speech, note card above it, red X away from the speaker"]
    FX --> GES["Gesture table: event kind to clip"]
    GES --> PQ["Priority queue, depth 2, with envelopes"]
    PQ --> MIX["Animation mixer: base clip plus gesture, facing, head glance, personality style"]
    FX --> FEEDUI["Event feed: one sentence per event"]
```

## 8. Research plan and gates

```mermaid
flowchart TD
    E0["E0 scripted controls: planted strategies, $0"] --> DEP["Memory-dependence check: memory must beat amnesic"]
    DEP --> G0{"G0"}
    G0 -->|fails today: memory costs more than it returns| FIX["Fix how memory is used; rerun"]
    FIX --> DEP
    G0 -->|passes| SMOKE["Live smoke test"]
    SMOKE --> PILOT["E1 pilot, 10 seeds"]
    PILOT --> G1{"G1: self-edited memory beats equal compute?"}
    G1 -->|no| DIAG["Diagnose: prompt, tier, feedback, horizon"]
    G1 -->|yes| E1["E1 at 20 seeds"]
    E1 --> E5["E5 transplant: does the practice carry the benefit?"]
    E5 --> G3{"G3"}
    G3 -->|yes| S3["E4 capability ladder, E3a protocol transplant, E7 persistence channel"]
    S3 --> S4["E3 ratchet at 42 days, E8 control levels, replications"]
    S4 --> PAPER["Paper and release"]
```

## 9. Live Claude test

```mermaid
flowchart LR
    KEY{"Workspace-scoped key in the environment?"} -->|no| WAIT["Blocked: add ANTHROPIC_API_KEY in the environment settings"]
    KEY -->|yes| GUARD["Budget guard: sum of real spend so far plus the next run's cap must stay under the budget"]
    GUARD --> S1["Stage 1: smoke on each model"]
    S1 --> S2["Stage 2: Claude ladder behaviour demo"]
    S2 --> S3["Stage 3: live memory-dependence check, paired seeds"]
    S3 --> S4["Stage 4: scarcity and deception, paired seeds"]
    S4 --> REP["Results into the proposal and the vault"]
```
