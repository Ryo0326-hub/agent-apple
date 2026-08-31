# Frozen event universe and sources

The schedule is intentionally curated before trading. First-party event announcements establish the event; Alpaca MCP option-contract and chain reads establish whether the required expirations are tradeable under ThetaTrap's frozen policy.

## Eligibility summary

| Date | Symbol | Status | Reason |
| --- | --- | --- | --- |
| 2026-09-01 | PANW | Eligible | First-party event verified |
| 2026-09-01 | MDB | Eligible | First-party event verified |
| 2026-09-01 | CRDO | Eligible | First-party event verified |
| 2026-09-01 | GTLB | Eligible | First-party event verified |
| 2026-09-01 | DELL | Excluded | `RELEASE_TIME_AMBIGUOUS` |
| 2026-09-02 | AVGO | Eligible | First-party event verified |
| 2026-09-02 | SNOW | Eligible | First-party event verified |
| 2026-09-02 | NTAP | Excluded | `REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE` |
| 2026-09-02 | AI | Eligible | First-party event verified |

Final count: **7 eligible, 2 excluded**.

## First-party announcements

- [Palo Alto Networks — fiscal fourth-quarter and fiscal-year 2026 results announcement](https://investors.paloaltonetworks.com/news-releases/news-release-details/palo-alto-networks-announce-fiscal-fourth-quarter-and-fiscal-7)
- [MongoDB — second-quarter fiscal 2027 earnings date](https://investors.mongodb.com/news-releases/news-release-details/mongodb-inc-announces-date-second-quarter-fiscal-2027-earnings)
- [Credo — first-quarter fiscal 2027 results conference call](https://investors.credosemi.com/news-events/news/news-details/2026/Credo-Schedules-First-Quarter-Fiscal-Year-2027-Financial-Results-Conference-Call/default.aspx)
- [GitLab — second-quarter fiscal 2027 results announcement](https://ir.gitlab.com/news/news-details/2026/GitLab-To-Announce-Second-Quarter-Fiscal-2027-Financial-Results/default.aspx)
- [Dell Technologies — second-quarter fiscal 2027 results notice](https://investors.delltechnologies.com/node/20901)
- [Broadcom — third-quarter fiscal 2026 results announcement](https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announce-third-quarter-fiscal-year-2026-financial)
- [Snowflake — second-quarter fiscal 2027 results announcement](https://www.snowflake.com/en/news/press-releases/snowflake-to-announce-financial-results-for-the-second-quarter-of-fiscal-2027/)
- [NetApp — first-quarter fiscal 2027 results webcast](https://investors.netapp.com/news/news-details/2026/NetApp-Hosts-First-Quarter-of-Fiscal-Year-2027-Financial-Results-Webcast/default.aspx)
- [C3 AI — first-quarter fiscal 2027 results announcement](https://c3ai.gcs-web.com/news-releases/news-release-details/c3-ai-announce-financial-results-fiscal-first-quarter-2027)

## Exclusion evidence

### DELL — `RELEASE_TIME_AMBIGUOUS`

The first-party notice provides a 16:30 ET conference-call time but does not supply sufficient evidence that the results release itself occurs after the close at the frozen event time. ThetaTrap fails closed rather than inferring a release time from the call.

### NTAP — `REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE`

The event itself is first-party verified, but the strategy requires both September 4 and September 11 expirations for its front/back volatility comparison. Alpaca MCP option-contract discovery did not list either required weekly expiration; the nearest observed standard expiration was September 18. NTAP is therefore structurally ineligible even though its event is verified.

## Supporting references

- [Alpaca options level 3 and multi-leg trading](https://docs.alpaca.markets/us/docs/options-level-3-trading)
- [Alpaca historical options data](https://docs.alpaca.markets/us/docs/historical-option-data)
- [Alpaca paper-trading limitations](https://docs.alpaca.markets/us/docs/paper-trading)
- [Official Alpaca MCP server](https://github.com/alpacahq/alpaca-mcp-server)
- [BLS September 2026 release calendar](https://www.bls.gov/schedule/2026/09_sched_list.htm)

These references define the frozen competition input. They are not evidence of strategy profitability.
