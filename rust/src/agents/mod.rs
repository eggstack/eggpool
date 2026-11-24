//! Embedded reverse-tunnel ownership for per-user provider agents.

pub mod tunnel;

pub use tunnel::{
    AgentTunnelCloseReport, AgentTunnelError, AgentTunnelManager, AgentTunnelSet,
    AgentTunnelSnapshot, AgentTunnelStatus,
};
