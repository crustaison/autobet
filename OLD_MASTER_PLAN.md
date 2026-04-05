
# Modular Multi-Venue Prediction Market Platform
## Master Implementation Plan
### Revised and Expanded Markdown Spec

This document is the cleaned-up master plan for building a browser-based, LAN-accessible, modular prediction market platform.

It keeps the strongest parts of the original architecture:
- modular design
- crypto-first launch
- observe, paper, and live execution separation
- grounded chat
- tooltips and onboarding
- multiple venues
- multiple model providers
- future module expansion without rewriting the core

It also folds in the improvements discussed afterward:
- configurable decision engine selection instead of one fixed engine
- historical data setup during onboarding
- CSV, JSON, SQLite dataset, and optional vector retrieval support
- compatibility-aware settings and guided configuration
- resettable paper-trading runs with isolated experiment history
- venue fee profiles built in by default
- premium UX and design-system guidance
- an AutoAgent-style orchestration layer
- an autoresearch-style experiment loop with keep-or-revert discipline
- stronger audit, replay, versioning, and data-lineage rules

---

# 1. Primary Goal

Build a platform that can:

1. collect market data
2. collect outside reference data
3. store and import historical data
4. build decision windows
5. generate trading recommendations
6. run in observe mode
7. run in paper trading mode
8. run in live trading mode
9. explain decisions in a dashboard
10. let the operator add and edit API keys in settings
11. let the operator switch decision engines in the dashboard
12. let the operator switch models in the dashboard
13. let the operator switch per-market-group execution modes in the dashboard
14. let the operator control stake policies, fee profiles, and paper-trading profiles
15. let the operator compare engines, models, runs, and venues
16. allow future market modules to plug in cleanly
17. be usable by opening it in a browser on the network

---

# 2. Main Architectural Rule

Do not build a one-off crypto script.

Do not build a Kalshi-only script.

Do not hardcode one decision method forever.

Do not hardcode MiniMax or any other model everywhere.

Do not build a system where research, execution, dashboard, onboarding, and chat are tangled together.

Instead, build a **core platform** with **registries**, **adapters**, **profiles**, **modules**, **decision engines**, **risk logic**, and **execution logic** separated cleanly.

The system should be made of these major pieces:

1. Core backend service
2. Frontend dashboard
3. Venue adapters
4. Reference data providers
5. Historical data import and dataset registry
6. Market modules
7. Model provider adapters
8. Decision engine registry
9. Compatibility engine
10. Risk engine
11. Execution engine
12. Chat and explanation layer
13. Onboarding and settings layer
14. Paper-trading run manager
15. Audit, health, replay, and lineage layer
16. Agent orchestration and experiment layer

---

# 3. Product Vocabulary

Use these exact meanings in the database, code, UI, docs, tooltips, and chat.

## 3.1 Venue
A place where contracts exist and where trades may be placed.

Examples:
- Kalshi
- Polymarket
- future venues later

## 3.2 Reference Authority
An outside truth source used to understand the underlying market or condition.

Examples:
- Coinbase spot BTC/USD for crypto
- sports score feed for sports
- oil market data feed for oil
- polling/context source for elections
- mention-count source for mentions

## 3.3 Module
A domain-specific plugin that understands one family of markets.

Examples:
- crypto
- oil
- sports
- elections
- mentions

## 3.4 Market Group
A configurable unit inside a module.

Examples:
- BTC
- ETH
- SOL
- XRP
- WTI
- NBA
- NFL
- elections-presidential
- mentions-brand-x

## 3.5 Observe Mode
Collect data and generate signals, but do not create paper or live trades.

## 3.6 Paper Mode
Use real live data and simulate trades, but do not place real orders.

## 3.7 Live Mode
Use real live data and place real orders if safety checks pass.

## 3.8 Trade Venue
The venue where orders are actually placed or would be placed.

## 3.9 Observe Venues
Extra venues watched for pricing comparison and cross-market signal context.

## 3.10 Model Provider
A service that can run a model.

Examples:
- MiniMax API
- local OpenAI-compatible endpoint
- local custom model service

## 3.11 Model Profile
A specific model under a provider.

Examples:
- MiniMax M2.1
- MiniMax M2.5
- MiniMax M2.7
- local-qwen
- local-llama

## 3.12 Decision Engine
A strategy engine that converts decision windows into YES, NO, or PASS recommendations.

Examples:
- raw_vector_knn
- rules_engine
- hybrid_vector_rules
- custom_engine
- shadow_engine

## 3.13 Feature Profile
A named definition of which features are calculated and in what order.

## 3.14 Dataset
An imported or collected historical data source used for backfill, comparison, calibration, or replay.

## 3.15 Dataset Usage Mode
How imported history may be used.

Examples:
- backfill_only
- calibration_allowed
- compare_only
- shadow_only

## 3.16 Compatibility Engine
A rules layer that determines which settings, engines, profiles, datasets, and venues work together.

## 3.17 Paper Trading Profile
A saved profile that defines how simulated capital, stake sizing, and fee/slippage assumptions should work.

## 3.18 Paper Run
A specific isolated experiment instance under a paper-trading profile.

## 3.19 Fee Profile
A versioned definition of how a venue’s fees are estimated, imported, or applied to trades.

## 3.20 Tooltip
A mouseover or info popup explaining what a setting or value means.

Tooltips must be:
- on by default
- toggleable in settings

## 3.21 Decision Packet
A structured record explaining why the bot chose YES, NO, or PASS.

## 3.22 Replay Mode
A mode that replays historical data in time order using only information that would have been available at that time.

## 3.23 Agent Orchestration Layer
A non-execution assistant layer used for onboarding help, compatibility guidance, import assistance, experiment setup, and grounded explanation.

## 3.24 Autoresearch Loop
A measurable experiment loop that changes one configuration or engine parameter at a time, runs a test, scores the result, and keeps or reverts the change.

---

# 4. Technology Choices

## 4.1 Backend
Use **Go**.

Reasons:
- good for long-running services
- strong concurrency
- simple deployment
- strong HTTP support
- good fit for polling, orchestration, risk checks, and execution
- good fit for a single local process or service

## 4.2 Research and Experimentation
Use **Python** for experiments, analysis, and offline research tooling.

Reasons:
- faster experimentation
- easier eval loops
- easier analysis
- easier backtest tooling
- easier prototyping for research agents

## 4.3 Database
Use **SQLite** first as the system of record.

Reasons:
- simple
- durable
- transactional
- easy to back up
- excellent for append-only records, configs, logs, decisions, and trades

## 4.4 Vector Retrieval
Do **not** make a vector database the primary source of truth.

Vector storage is optional and should be used only when the selected engine benefits from it.

Use:
- SQLite only for v1 if desired
- optional vector-sync layer later if needed

## 4.5 Frontend
Use a browser frontend with a premium design system.

A framework choice can be:
- SvelteKit
- React
- Vue

Choose one and stay consistent.

Do not let frontend choices break the product architecture.

---

# 5. High-Level Architecture

Split the platform into layers.

## 5.1 Core Backend Service
This handles:
- serving HTTP API
- auth
- onboarding
- settings
- provider registry
- model registry
- decision engine registry
- feature profile registry
- market-group registry
- dataset registry
- polling loops
- decision loops
- risk checks
- execution
- health checks
- audit logs
- lineage records
- chat context generation

## 5.2 Frontend Dashboard
This handles:
- login
- onboarding wizard
- provider setup
- settings editing
- model switching
- engine switching
- market-group mode switching
- stake policy editing
- fee profile editing
- paper-run management
- dataset import and mapping
- decision browsing
- trade browsing
- compatibility guidance
- health view
- tooltip rendering
- popup chat
- replay mode interface

## 5.3 Venue Adapters
Each venue adapter handles:
- listing contracts
- fetching contract snapshots
- fetching order books if available
- checking balances
- checking positions
- placing orders
- cancelling orders
- reading settlements
- reading or estimating fee data where possible

## 5.4 Reference Data Providers
Each reference provider handles:
- fetching outside truth data
- returning normalized snapshots
- providing recent history for features

## 5.5 Historical Data Import Layer
This handles:
- CSV import
- JSON import
- SQLite dataset import
- optional vector import
- schema validation
- field mapping
- symbol mapping
- timezone normalization
- duplicate detection
- preview before commit
- merge versus isolate behavior

## 5.6 Market Modules
Each module handles:
- identifying relevant contracts
- mapping contracts to market groups
- defining required reference providers
- building feature vectors
- building explanation context
- defining optional research hooks

## 5.7 Model Provider Layer
Each model provider handles:
- validation
- chat calls
- explanation calls
- decision review calls
- optional structured JSON output calls

## 5.8 Decision Engine Registry
Each decision engine handles:
- validation
- requirements
- recommendation generation
- confidence calculation
- expected value logic
- pass logic
- optional shadow comparison behavior

## 5.9 Compatibility Engine
This handles:
- requires
- supports
- conflicts_with
- optional_with
- recommended_with
- UI highlighting of valid combinations
- warnings before save
- auto-suggesting missing dependencies

## 5.10 Risk Engine
This handles:
- safety gating
- max exposure
- max losses
- kill switches
- cooldowns
- permission checks
- fee-aware edge gating
- slippage-aware edge gating

## 5.11 Execution Engine
This handles:
- observe behavior
- paper behavior
- live behavior
- fee estimation
- slippage handling
- fill-state handling

## 5.12 Paper Run Manager
This handles:
- paper profiles
- bankroll scope
- starting capital
- run isolation
- reset behavior
- run comparison
- archival of completed runs

## 5.13 Chat Layer
This handles:
- grounded explanations
- system chat
- decision chat
- trade chat
- market-group chat
- config compatibility explanations
- import explanations

## 5.14 Agent Orchestration Layer
This handles:
- onboarding assistance
- compatibility explanations
- import wizard help
- experiment launch suggestions
- grounded operator assistance
- no autonomous live execution

## 5.15 Autoresearch Experiment Layer
This handles:
- one-change-at-a-time experiments
- measurable scoring
- keep-or-revert logic
- replay-mode evaluations
- paper-mode evaluations
- experiment tracking

---

# 6. Core Rules About Data Collection

If a market group is enabled, data collection should continue whether live trading is on or off.

That means:
- observe collects data
- paper collects data
- live collects data
- disabled is the only mode that fully stops the group

This is necessary because:
- paper trading needs real data
- live readiness needs stored history
- chat needs stored evidence
- postmortems need stored evidence
- model tuning needs stored outcomes
- compatibility decisions need sufficiency checks
- replay mode needs clean history

---

# 7. Core Rules About Live Trading

Live trading must not be controlled by just one switch.

Use two separate permission layers.

## 7.1 Global Live Toggle
A system-wide master toggle.

Default:
- OFF

## 7.2 Per Market Group Mode
Each group can be:
- disabled
- observe
- paper
- live

Real orders should only happen if:
- global live toggle is ON
- market group mode is LIVE
- venue is healthy
- provider is healthy
- credentials are valid
- fee profile is valid
- risk checks pass
- kill switch is not active
- the config is marked live-compatible
- human approval rules are satisfied

---

# 8. Build Philosophy

Build generic architecture.
Implement crypto first.
Add future modules later.

Meaning:
- design everything to be modular
- only fully implement crypto first
- keep placeholders for oil, sports, elections, mentions
- keep multiple engines possible from the beginning
- ship one good engine first, not twenty half-finished ones

---

# 9. Repository Structure

Create the repo like this:

```text
/cmd/bot
/internal/auth
/internal/onboarding
/internal/settings
/internal/store
/internal/audit
/internal/health
/internal/compat
/internal/datasets
/internal/imports
/internal/fees
/internal/paper
/internal/replay
/internal/experiments
/internal/agents
/internal/providers
/internal/providers/minimax
/internal/providers/localmodel
/internal/venues
/internal/venues/kalshi
/internal/venues/polymarket
/internal/reference
/internal/reference/coinbase
/internal/modules
/internal/modules/crypto
/internal/modules/oil
/internal/modules/sports
/internal/modules/elections
/internal/modules/mentions
/internal/marketgroups
/internal/features
/internal/decision
/internal/decision/engines
/internal/execution
/internal/chat
/internal/models
/internal/web
/internal/risk
/frontend
/migrations
/scripts
/docs
```

---

# 10. Database Schema

Use SQLite. Create migrations. Build the tables below.

## 10.1 users
Fields:
- id
- username
- password_hash
- role
- created_at
- updated_at

## 10.2 system_state
Fields:
- id
- onboarding_complete
- global_live_enabled
- initialized_at
- updated_at

## 10.3 providers
Fields:
- id
- provider_key
- provider_type
- display_name
- enabled
- config_json
- created_at
- updated_at

## 10.4 secrets
Fields:
- id
- provider_id
- secret_key
- encrypted_value
- created_at
- updated_at

## 10.5 model_profiles
Fields:
- id
- name
- provider_id
- model_identifier
- purpose
- enabled
- config_json
- created_at
- updated_at

## 10.6 decision_engines
Fields:
- id
- engine_key
- display_name
- engine_type
- enabled
- config_json
- created_at
- updated_at

## 10.7 feature_profiles
Fields:
- id
- profile_key
- display_name
- module_key
- feature_schema_json
- enabled
- created_at
- updated_at

## 10.8 modules
Fields:
- id
- module_key
- display_name
- enabled
- config_json
- created_at
- updated_at

## 10.9 stake_policies
Fields:
- id
- name
- min_stake
- max_stake
- policy_json
- created_at
- updated_at

## 10.10 fee_profiles
Fields:
- id
- venue_key
- name
- effective_date
- fee_formula_type
- fee_config_json
- source
- created_at
- updated_at

## 10.11 fee_profile_versions
Fields:
- id
- fee_profile_id
- version_label
- config_json
- effective_date
- created_at

## 10.12 paper_profiles
Fields:
- id
- name
- starting_capital
- capital_scope
- sizing_mode
- config_json
- created_at
- updated_at

## 10.13 paper_runs
Fields:
- id
- paper_profile_id
- name
- status
- starting_capital
- current_capital
- config_snapshot_json
- started_at
- ended_at
- reset_reason
- created_at

## 10.14 datasets
Fields:
- id
- dataset_key
- display_name
- module_key
- source_type
- usage_mode
- schema_version
- time_range_start
- time_range_end
- quality_score
- metadata_json
- created_at
- updated_at

## 10.15 dataset_versions
Fields:
- id
- dataset_id
- version_label
- metadata_json
- created_at

## 10.16 dataset_import_jobs
Fields:
- id
- dataset_id
- source_type
- file_name
- status
- mapping_json
- quality_report_json
- created_at
- updated_at

## 10.17 market_groups
Fields:
- id
- module_id
- group_key
- display_name
- enabled
- execution_mode
- trade_venue_key
- reference_authority_key
- model_profile_id
- decision_engine_id
- feature_profile_id
- stake_policy_id
- fee_profile_id
- paper_profile_id
- config_json
- created_at
- updated_at

## 10.18 market_group_observe_venues
Fields:
- id
- market_group_id
- venue_key
- created_at

## 10.19 contracts
Fields:
- id
- venue_key
- external_contract_id
- module_id
- market_group_id
- title
- subtitle
- status
- close_time
- settle_time
- metadata_json
- created_at
- updated_at

## 10.20 contract_snapshots
Fields:
- id
- contract_id
- venue_key
- snapshot_ts
- yes_bid
- yes_ask
- no_bid
- no_ask
- book_json
- created_at

## 10.21 reference_snapshots
Fields:
- id
- source_key
- market_group_id
- snapshot_ts
- payload_json
- created_at

## 10.22 decision_windows
Fields:
- id
- contract_id
- market_group_id
- window_ts
- feature_profile_id
- features_json
- vector_blob
- outcome
- metadata_json
- created_at

## 10.23 decisions
Fields:
- id
- contract_id
- market_group_id
- paper_run_id
- execution_mode
- model_profile_id
- decision_engine_id
- decision_json
- explanation_text
- created_at

## 10.24 trades
Fields:
- id
- decision_id
- contract_id
- paper_run_id
- execution_mode
- side
- quantity
- stake
- price
- fill_status
- fee_profile_id
- fee_profile_version_id
- estimated_entry_fee
- actual_entry_fee
- estimated_exit_fee
- actual_exit_fee
- total_estimated_fee
- total_actual_fee
- gross_pnl
- net_pnl
- pnl
- fee_payload_json
- created_at
- updated_at

## 10.25 replay_runs
Fields:
- id
- name
- module_key
- time_range_start
- time_range_end
- config_snapshot_json
- status
- created_at
- updated_at

## 10.26 compatibility_rules
Fields:
- id
- object_type
- object_key
- rule_type
- rule_json
- created_at
- updated_at

## 10.27 chat_sessions
Fields:
- id
- user_id
- context_type
- context_ref_id
- created_at
- updated_at

## 10.28 chat_messages
Fields:
- id
- session_id
- role
- message_text
- metadata_json
- created_at

## 10.29 audit_logs
Fields:
- id
- actor_type
- actor_id
- event_type
- object_type
- object_id
- payload_json
- created_at

## 10.30 lineage_records
Fields:
- id
- record_type
- record_id
- source_type
- source_ref
- metadata_json
- created_at

## 10.31 user_preferences
Fields:
- id
- user_id
- tooltips_enabled
- theme
- refresh_interval
- created_at
- updated_at

Defaults:
- tooltips_enabled = true

---

# 11. HTTP Server

Create a Go HTTP server that:
- binds to `0.0.0.0`
- serves API routes
- serves the frontend
- supports LAN browser access

Main API route groups:
- `/api/auth`
- `/api/onboarding`
- `/api/settings`
- `/api/providers`
- `/api/models`
- `/api/engines`
- `/api/modules`
- `/api/datasets`
- `/api/imports`
- `/api/fees`
- `/api/paper`
- `/api/replay`
- `/api/market-groups`
- `/api/contracts`
- `/api/decisions`
- `/api/trades`
- `/api/chat`
- `/api/health`
- `/api/compat`
- `/api/experiments`

---

# 12. Authentication

Implement local auth first.

Requirements:
- login page
- password hashing
- admin role
- session auth or token auth
- logout

Keep it simple initially.

Need:
- one admin user during onboarding
- support multiple users later if wanted

---

# 13. Onboarding Flow

On first run, redirect to onboarding.

Onboarding must be step-by-step.

## 13.1 Create Admin User
Ask for:
- username
- password

Create the first admin user.

## 13.2 Network Settings
Ask for:
- bind address
- port
- optional system display name

Defaults:
- bind address = `0.0.0.0`
- port = `7778`

## 13.3 Add Providers
Collect and validate:
- Kalshi credentials
- Coinbase config
- MiniMax key
- optional Polymarket credentials
- optional local model endpoint config

For each provider:
- save config
- test connection
- show status

## 13.4 Historical Data Setup
Ask:
- no historical data yet
- CSV import
- JSON import
- SQLite dataset import
- vector retrieval support
- mixed mode

Explain each option clearly.

If the user chooses imported history, ask:
- what the file contains
- how it should be used
- backfill only, calibration allowed, compare only, or shadow only

## 13.5 Create Model Profiles
Allow the user to define multiple models.

Examples:
- MiniMax M2.1 for chat
- MiniMax M2.5 for summary
- MiniMax M2.7 for review

## 13.6 Create Decision Engine Profiles
Allow the user to define or select:
- Raw Vector KNN
- Rules Engine
- Hybrid Vector + Rules
- Shadow Engine
- future custom engines

Important:
Do not make raw vector the only allowed choice.

## 13.7 Configure Paper Trading Defaults
Ask for:
- starting capital
- capital scope
- fixed stake or dynamic
- whether fees are realistic or zero
- whether compounding is enabled
- reset behavior preference

## 13.8 Configure Market Groups
Start with crypto groups:
- BTC
- ETH
- SOL
- XRP

For each group let the user choose:
- enabled yes/no
- execution mode
- trade venue
- observe venues
- reference authority
- decision engine
- feature profile
- model profile
- stake policy
- fee profile
- paper profile

## 13.9 Fee Profiles
Preload default fee profiles:
- Kalshi general
- Kalshi maker
- Kalshi special lower-fee index profile
- Polymarket crypto category
- Polymarket category-based generic profile

Let the user review them.

## 13.10 Compatibility Review
Show which choices are:
- compatible
- missing requirements
- warning
- experimental
- ready for observe
- ready for paper
- ready for live

## 13.11 Safety Confirmation
Warn clearly:
- live trading is OFF by default
- global live toggle starts OFF
- groups should start in observe or paper
- imported history does not prove live edge
- autoresearch loops must never directly promote configs to live

## 13.12 Health Checks
Run:
- DB write test
- provider connectivity tests
- model response tests
- reference data response tests
- venue auth tests
- fee profile validation
- compatibility validation

## 13.13 Finish
Set:
- onboarding_complete = true

Then redirect to dashboard.

---

# 14. Settings Pages

Create settings sections for:

## 14.1 General
- system name
- timezone
- bind address
- port
- refresh interval

## 14.2 Security
- users
- password changes
- session timeout

## 14.3 Providers
- Kalshi
- Polymarket
- Coinbase
- MiniMax
- local models

## 14.4 Models
- model profiles
- global default chat model
- global default reasoning model
- global default explanation model
- shadow model

## 14.5 Decision Engines
- engine profiles
- feature profiles
- similarity metric
- history requirements
- shadow engine
- experimental flag

## 14.6 Market Groups
- enable/disable
- execution mode
- trade venue
- observe venues
- reference authority
- decision engine
- feature profile
- model profile
- stake policy
- fee profile
- paper profile
- risk caps

## 14.7 Datasets
- imported datasets
- import jobs
- mappings
- quality reports
- usage mode

## 14.8 Paper Trading
- paper profiles
- current runs
- run history
- reset rules
- compare runs

## 14.9 Fees
- fee profiles
- fee versions
- realistic versus zero-fee mode
- slippage assumptions

## 14.10 UI
- theme
- refresh interval
- tooltip toggle
- density
- animation level

Important:
Add a settings toggle:
- label: `Enable Tooltips`
- type: boolean
- default: ON

Persist per user if possible.

---

# 15. Tooltip System

Tooltips are not optional. They are part of the product.

Create a centralized tooltip registry.

## 15.1 Tooltip Keys
Create entries for:
- execution_mode
- observe_mode
- paper_mode
- live_mode
- trade_venue
- observe_venues
- reference_authority
- decision_engine
- feature_profile
- paper_profile
- fee_profile
- stake_policy
- edge
- confidence
- expected_value
- shadow_model
- provider
- module
- market_group
- global_live_enabled
- max_daily_exposure
- kill_switch
- replay_mode
- compatibility_status

## 15.2 Tooltip Rendering Rules
- show on mouseover
- support info icon trigger
- hide globally if tooltips_enabled is false

---

# 16. Decision Engine System

Do not use only one hardcoded engine.

Create a decision engine registry.

## 16.1 Engine Types
Support:
- raw_vector_knn
- rules_engine
- hybrid_vector_rules
- custom_engine
- shadow_engine

## 16.2 Raw Vector KNN
Use normalized raw feature vectors with cosine similarity and weighted voting.

This is a valid v1 engine.
It is not the only valid future engine.

## 16.3 Rules Engine
A simpler fallback engine that can work with little or no labeled history.

## 16.4 Hybrid Engine
Combines rule gating with vector similarity or future comparison logic.

## 16.5 Data Sufficiency Rules
Each engine must declare:
- minimum labeled windows
- required feature profile
- required data sources
- whether replay mode is supported
- whether live mode is allowed

If requirements are not met:
- disable the engine
- or allow it only in observe or shadow mode

## 16.6 Shadow Engine Support
Allow one active engine and one shadow engine.

Store:
- active recommendation
- shadow recommendation
- agreement or divergence
- later performance comparison

---

# 17. Historical Data and Dataset System

Saved history data must be a first-class subsystem.

## 17.1 Supported Sources
Support:
- live-collected history
- CSV imports
- JSON imports
- SQLite dataset imports
- optional vector imports
- merged history

## 17.2 Supported Usage Modes
Support:
- backfill only
- calibration allowed
- compare only
- shadow only

## 17.3 Import Wizard Requirements
Every import should support:
- schema detection
- field mapping
- symbol mapping
- timezone normalization
- duplicate detection
- preview before commit
- quality checks
- merge versus isolate mode

## 17.4 Do Not Let Imported Data Silently Contaminate Live Evaluation
Every dataset must declare:
- source
- time range
- usage mode
- quality score
- schema version
- whether labels are trusted

---

# 18. Compatibility Engine

A robust system must actively prevent broken combinations.

## 18.1 Each Component Should Declare
- requires
- supports
- conflicts_with
- optional_with
- recommended_with

## 18.2 UI Behavior
The UI should:
- gray out incompatible options
- highlight valid options
- show warnings before save
- explain why something is unavailable
- auto-suggest missing requirements

## 18.3 Compatibility Status Labels
Show:
- Compatible
- Missing Requirement
- Warning
- Experimental
- Not Recommended for Live
- Ready for Observe
- Ready for Paper
- Ready for Live

---

# 19. Venue Adapters

Start with Kalshi first.
Add Polymarket next.
Leave room for others later.

A market group should support:
- one trade venue
- zero or more observe venues
- one reference authority

This makes these cases possible:
- trade Kalshi, observe Polymarket, use Coinbase as reference
- trade Polymarket, observe Kalshi, use Coinbase as reference

---

# 20. Default Fee Profiles

Build venue fees in by default, but keep them versioned and editable.

## 20.1 Kalshi Default Fee Profiles
Support:
- general trading fee profile
- maker fee profile
- lower-fee special profile where applicable
- zero settlement fee default

Use the current documented venue schedule as the default loaded profile.

## 20.2 Polymarket Default Fee Profiles
Support:
- maker = zero
- taker fees by market category
- category-based default profiles
- crypto-specific default profile for crypto market groups

## 20.3 Fee Profile Rules
Every trade, paper trade, replay trade, and imported trade record should carry:
- fee profile used
- fee version used
- estimated fee
- actual fee if known
- gross PnL
- net PnL

## 20.4 Fee-Aware Gating
The risk engine must be able to ask:
- does this trade still have positive expected value after estimated fees
- does this trade still have positive expected value after estimated slippage

---

# 21. Snapshot Storage

Persist all useful market and reference snapshots.

## 21.1 Contract Snapshots
Store:
- timestamp
- yes bid
- yes ask
- no bid
- no ask
- book if available
- venue

## 21.2 Reference Snapshots
Store:
- timestamp
- source
- normalized reference payload

Why:
- research
- paper evaluation
- replay
- explanations
- postmortems
- future model tuning

---

# 22. Decision Windows

A decision window is the normalized record used by the bot.

It should combine:
- trade venue pricing
- observe venue data
- reference authority data
- derived features
- future outcome later when known

## 22.1 Decision Window Fields
- contract id
- market group id
- timestamp
- feature profile id
- features_json
- vector_blob
- metadata_json
- outcome

---

# 23. Crypto Module First

Crypto is the first full module.

## 23.1 Crypto Module Responsibilities
- identify crypto contracts
- map to BTC, ETH, SOL, XRP groups
- fetch Coinbase reference data
- combine venue and outside data
- build features
- build explanation packet

## 23.2 Raw Vector KNN Example
Use:
- normalized vectors
- cosine similarity
- nearest-neighbor retrieval
- weighted voting
- confidence thresholds
- pass logic

This is the initial engine example, not an eternal rule.

## 23.3 Placeholder Modules
Create stubs now for:
- oil
- sports
- elections
- mentions

---

# 24. Stake Policy System

Do not use a single fixed stake number.

Use stake policies.

## 24.1 Stake Policy Fields
- min stake
- max stake
- edge thresholds
- confidence thresholds
- optional mode-specific rules
- optional group overrides
- Kelly fraction if relevant
- capital scope awareness

## 24.2 Important Rule
Do not pick sizes randomly.

Stake should depend on:
- edge
- confidence
- risk caps
- current exposure
- current paper or live bankroll rules
- fee-adjusted expected value
- slippage assumptions

---

# 25. Paper Trading System

Paper mode is critical.

Paper trading is both:
- simulated execution
- experiment infrastructure

## 25.1 Paper Profiles
Each profile should define:
- starting capital
- capital scope
- fixed stake or dynamic stake
- per-trade cap
- per-group cap
- compounding on or off
- realistic fees versus zero-fee mode
- slippage mode
- reset behavior

## 25.2 Capital Scope Options
Support:
- global paper bankroll
- per market group bankroll
- per coin bankroll
- fixed stake only
- profile-based custom bankroll

## 25.3 Paper Run Isolation
Every paper session should have:
- run_id
- profile_id
- config snapshot
- engine snapshot
- feature profile snapshot
- stake policy snapshot
- dataset snapshot
- fee profile snapshot
- start timestamp
- end timestamp
- reset reason

Do not mix old and new experimental results together.

## 25.4 Reset Types
Support:
- soft reset
- config reset
- bankroll reset only
- full archive and restart
- delete test run

Default behavior should be archival reset, not destructive deletion.

## 25.5 Compare Runs
Allow:
- current run versus prior run
- same config versus different config
- gross versus net results
- with and without fees
- with and without slippage

---

# 26. Replay Mode

Replay mode must exist separately from paper trading.

## 26.1 Replay Purpose
Use replay mode to:
- test engines honestly on past data
- compare configs
- audit lookahead problems
- evaluate imported datasets
- run autoresearch loops safely

## 26.2 Replay Rules
The system must:
- only expose information available up to that replay timestamp
- prevent lookahead leakage
- store replay results separately from paper runs

---

# 27. Live Execution Engine

Keep live execution separate from paper execution.

## 27.1 Live Execution Rule
Only allow live trades if:
- global_live_enabled = true
- market group mode = live
- venue healthy
- credentials valid
- fee profile valid
- risk checks pass
- decision passes gating
- kill switch not active
- config is marked live-compatible

## 27.2 Logging
For every live trade store:
- request
- response
- timestamps
- failures
- fill status
- settlement result
- estimated fees
- actual fees
- gross PnL
- net PnL

---

# 28. Risk Engine

The risk engine protects the system.

## 28.1 Global Risk Controls
- max daily loss
- max daily exposure
- emergency pause
- global live toggle
- provider degradation pause

## 28.2 Per Group Risk Controls
- max stake
- max daily exposure
- max concurrent trades
- cooldown after loss streak
- kill switch

## 28.3 Portfolio-Level Controls
Also add:
- cross-market correlation caps
- venue concentration caps
- daily notional caps
- macro-bet concentration checks

## 28.4 Fee and Slippage Awareness
The risk engine must be able to reject a trade if:
- estimated edge is too small after fees
- estimated edge is too small after slippage
- the signal is only gross-positive but net-negative

---

# 29. Chat Layer

The chat must be grounded in stored system records.

It must not behave like an ungrounded generic assistant.

## 29.1 Context Types
Support:
- system context
- market-group context
- decision context
- trade context
- run context
- replay context
- compatibility context

## 29.2 Chat Process
When a question is asked:
1. determine context
2. fetch stored facts
3. build an evidence packet
4. send that to the selected chat model
5. return grounded answer

If evidence is insufficient, the answer should say so.

---

# 30. Dashboard Pages

Build these pages.

## 30.1 Login
Simple login page.

## 30.2 Onboarding
Step-by-step setup wizard.

## 30.3 Overview
Show:
- provider health
- live toggle state
- observe, paper, and live counts
- recent decisions
- recent trades
- current paper runs
- net PnL summaries

## 30.4 Markets
Show:
- modules
- market groups
- enabled state
- execution mode
- trade venue
- observe venues
- reference authority
- decision engine
- feature profile
- model profile
- stake policy
- fee profile
- compatibility state

## 30.5 Providers
Show:
- provider status
- test actions
- last successful call
- last error

## 30.6 Models
Show:
- model profiles
- purpose
- provider
- current assignments

## 30.7 Engines
Show:
- engine profiles
- required history
- feature profile
- compatibility state
- shadow engine status

## 30.8 Datasets
Show:
- imported datasets
- usage mode
- time ranges
- quality scores
- lineage

## 30.9 Decisions
Show:
- recent decisions
- edge
- confidence
- reasoning
- pass versus trade
- active versus shadow outcome

## 30.10 Trades
Show:
- paper trades
- live trades
- replay trades
- fees
- net PnL

## 30.11 Paper Labs
Show:
- paper profiles
- current run
- starting bankroll
- current bankroll
- reset actions
- compare runs

## 30.12 Replay
Show:
- replay jobs
- config snapshots
- outcomes
- performance summaries

## 30.13 Chat
Popup or side panel.

## 30.14 Settings
Show:
- general
- security
- providers
- models
- engines
- market groups
- datasets
- paper trading
- fees
- UI preferences
- tooltip toggle

## 30.15 Health
Show:
- feed lag
- provider health
- model failures
- paused states
- stale data warnings
- data sufficiency warnings

---

# 31. Premium UX and Design System

The UX should have an ultra-premium look and feel.

That means:
- excellent spacing
- crisp typography
- consistent hierarchy
- soft motion
- subtle glass or layered surfaces
- clean tables
- elegant charts
- thoughtful empty states
- excellent loading states
- command palette
- dark mode done properly
- restrained use of color
- polished confirmation dialogs

## 31.1 Premium Feel Comes From Confidence
Every major control should preview its impact before save.

Example:
Switching BTC from Rules Engine to Raw Vector KNN will require labeled history, enable vector storage, and reset paper-run comparability.

## 31.2 Premium Feel Comes From Consistency
Create design tokens for:
- spacing
- radius
- elevation
- surface
- iconography
- badges
- animations
- density
- typography

---

# 32. Health Monitoring

The system should monitor itself.

Track health for:
- Kalshi provider
- Polymarket provider
- Coinbase provider
- MiniMax provider
- local model endpoints
- DB health
- polling loops
- decision loops
- execution loops
- import jobs
- replay jobs
- experiment jobs

Health states:
- healthy
- degraded
- failed
- paused

---

# 33. Audit, Lineage, and Versioning

Everything important should be logged and traceable.

Log:
- provider credential changes
- provider settings changes
- model changes
- engine changes
- feature profile changes
- market-group changes
- execution mode changes
- global live toggle changes
- stake policy changes
- fee profile changes
- paper profile changes
- paper reset changes
- dataset imports
- replay launches
- experiment launches
- tooltip toggle changes
- emergency pause changes
- kill switch changes

Every decision should be traceable to:
- snapshots used
- dataset version used
- engine used
- model used
- feature profile used
- fee profile used
- risk rules used

---

# 34. Agent Orchestration Layer

Use AutoAgent-style ideas only in controlled ways.

## 34.1 Good Uses
Use the agent orchestration layer for:
- onboarding help
- compatibility explanations
- import assistance
- config recommendations
- experiment suggestions
- grounded dashboard assistance

## 34.2 Forbidden Uses
Do not allow this layer to:
- directly place live trades
- silently change live configs
- self-promote experimental settings to live
- bypass policy gates

---

# 35. Autoresearch Layer

Use autoresearch-style ideas only in measurable, sandboxed ways.

## 35.1 Core Pattern
The loop should:
1. change one thing
2. run a paper or replay evaluation
3. score the result
4. keep the change if better
5. revert if worse

## 35.2 Good Uses
Use autoresearch loops for:
- threshold tuning
- feature profile tuning
- stake policy tuning
- fee-aware gating tuning
- slippage assumptions
- compatibility recommendation logic
- report generation experiments

## 35.3 Forbidden Uses
Do not allow the autoresearch loop to:
- rewrite the system without audit
- directly enable live mode
- evaluate only in-sample forever
- hide experimental changes from the operator

Replay mode should be the main safe evaluation surface.

---

# 36. Missing Edge-Case Protections That Must Be Included

Also include:
- slippage modeling
- partial fill handling
- canceled order handling
- stale-book handling
- voided and disputed market outcomes
- corrected settlement handling
- insufficient labeled-data handling
- lookahead-bias tests
- shifted-label tests
- delayed-entry perturbation tests
- regime breakdown reporting

---

# 37. Future Expansion Strategy

After crypto works, fill in more modules.

## 37.1 Oil Module
Add:
- oil contracts
- oil reference provider
- oil-specific features
- oil-specific explanations

## 37.2 Sports Module
Add:
- sports contracts
- score or game-state provider
- sports-specific features
- sports-specific explanations

## 37.3 Elections Module
Add:
- election contracts
- polling or context provider
- election-specific features
- election-specific explanations

## 37.4 Mentions Module
Add:
- mention-triggered contracts
- mention source
- mention-specific features
- explanations

No fundamental redesign should be needed.

---

# 38. Build Order

Build in this order:

1. create repo structure
2. create SQLite schema and migrations
3. build Go HTTP server
4. build auth
5. build onboarding
6. build settings pages
7. build tooltip system with default-on toggle
8. build provider registry
9. build model registry
10. build decision engine registry
11. build feature profile registry
12. build compatibility engine
13. build venue interfaces
14. build Kalshi adapter
15. build Polymarket adapter
16. build reference provider interfaces
17. build Coinbase provider
18. build module interface
19. build crypto module
20. create placeholder oil, sports, elections, and mentions modules
21. build dataset import system
22. build fee profile system
23. build paper profile and paper run system
24. build snapshot storage
25. build decision windows
26. build first real decision engine
27. build stake policy engine
28. build replay mode
29. build paper trading engine
30. build live execution engine
31. build risk engine
32. build dashboard pages
33. build decision detail view
34. build chat popup
35. build audit and lineage systems
36. run crypto in observe mode
37. move crypto to paper mode
38. compare runs
39. enable live only when ready
40. later add more modules and more engines

---

# 39. Detailed Engineering Notes for the First Real Engine

For the first real history-based engine:
- use normalized raw feature vectors
- use cosine similarity
- use weighted voting
- use recency decay
- use confidence thresholds
- use dead-zone pass logic
- store vectors as blobs for performance
- keep threshold values configurable
- do not treat example values as eternal truths

This keeps the strong implementation detail from the addendum without turning one engine into dogma.

---

# 40. Success Criteria

The generated code is acceptable if:

1. it compiles without errors
2. it starts without crashing
3. onboarding completes
4. provider validation works
5. contract and reference snapshots are stored
6. decision windows are created
7. at least one decision engine runs
8. paper profiles and paper runs work
9. reset creates new isolated runs
10. net PnL includes fee assumptions
11. compatibility guidance works
12. chat answers from stored evidence
13. replay mode can run a time-bounded evaluation
14. live mode remains gated behind explicit safety controls

---

# 41. Final Principles

Follow these rules:

1. the system must be modular
2. the core must not be crypto-specific
3. crypto is still the first real module
4. data collection continues even without live trading
5. paper mode comes before live
6. live requires both global and per-group enablement
7. tooltips exist throughout UI and can be turned off
8. providers, models, and decision engines are separate concepts
9. chat must be grounded in stored evidence
10. audit logs and lineage must cover critical changes
11. the dashboard must be usable over the network in a browser
12. dangerous actions must have explicit confirmations
13. paper experiments must be isolated by run
14. imported history must be versioned and usage-scoped
15. fee-aware net evaluation matters more than fantasy gross results
16. agent helpers may assist but must not bypass execution policy
17. autoresearch loops must be measurable, reversible, and sandboxed

---

# 42. Final One-Sentence Description

Build a LAN-accessible, browser-managed, modular prediction market platform that starts with crypto, supports observe, paper, replay, and live execution, supports multiple venues and model providers, supports multiple decision engines and historical data modes, explains itself through grounded chat, uses built-in fee-aware net evaluation, includes premium onboarding and compatibility guidance, and is structured so future modules like oil, sports, elections, and mentions can be added without rebuilding the core.
