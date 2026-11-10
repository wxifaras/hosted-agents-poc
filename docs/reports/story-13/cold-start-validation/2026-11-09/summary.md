# Story 13 - Hosted-agent startup: simple summary

## Summary

- We tested real deployed Hosted Agents in Microsoft Foundry, not a local simulation.
- The median first useful response was 6.39 seconds without snapshots and 1.46 seconds with snapshots: 77.1% less waiting.
- The agent application's preparation time was 4.264 seconds without snapshots and 0.135 seconds with snapshots.
- Adding precompiled Python recorded 1.36 seconds, compared with 1.46 seconds for snapshots alone. This small sample does not prove a repeatable extra gain.
- 63 of 63 planned cloud checks passed. 11 earlier failed diagnostic attempts are disclosed separately, not counted as successful comparisons.

## Conclusion

Memory snapshots were the main startup improvement in this controlled test. They are a good candidate for agents that keep required state outside the container. Validate the customer's real model and tools next, before promising end-to-end response times.

## Important limits

- Real Microsoft Foundry Hosted Agent deployments, with no AI model or external tool calls. These results measure startup, not the full response time of a customer workflow.
- With snapshots, changes to application memory and local files do not survive idle/resume. Store required state outside the agent container.
- Small, sequential samples do not establish p95, statistical significance, or a service guarantee.
