//! Operator TUI for Inference Autopilot: a presentation and approval client of the local
//! control-plane HTTP API. It holds no business logic and touches no infrastructure itself.
pub mod api;
pub mod app;
pub mod http;
pub mod model;
pub mod pipeline;
pub mod plain;
pub mod theme;
pub mod timeparse;
pub mod ui;
