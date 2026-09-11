# Deep Dive: Agent Integrations

Back to [Architecture](README.md)

`rust/src/operations/integrations.rs` owns `eggpool configsetup` output for
supported coding agents. It generates provider-neutral endpoint, model, and
secret references for each target without performing provider calls or
persisting credentials.

Integration generation is a CLI operation over the resolved configuration.
Generated files are written only when the operator requests an output path;
stdout modes remain suitable for review and shell piping. The native runtime
continues to serve all generated endpoints.
