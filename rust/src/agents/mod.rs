//! Embedded reverse-tunnel ownership and enrollment for provider agents.

pub mod enrollment;
pub mod ticket;
pub mod tunnel;

pub use enrollment::{EnrollmentRegistryError, EnrollmentReservation, PendingEnrollmentRegistry};
pub use ticket::{ControlAddress, EnrollmentTicket, TicketError};
pub use tunnel::{
    AgentTunnelAvailability, AgentTunnelCloseReport, AgentTunnelError, AgentTunnelManager,
    AgentTunnelSet, AgentTunnelSnapshot, AgentTunnelState, AgentTunnelStatus,
};
