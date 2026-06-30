# Domain → Channel Profile Map (batch mode)

The batch Excel has a **Domain** column but no platform column. Infer the channel
profile from the domain. `scripts/read_batch.py` applies this map; keep the two in sync.

| Domain | Channel profile | Notes |
|---|---|---|
| `linkedin.com` | LinkedIn | Long-form thought leadership; no Key Takeaways box. |
| `medium.com` | Medium | Conversational thought-leadership. |
| `damcogroup.com` (and the Damco blog) | SEO Articles | On-site SEO blog. |
| `sooperarticles.com` | SEO Articles | SEO-depth guest placement (full SEO rules). |
| `woxmax.com` | Guest Blog | External guest post. |
| **any other / unknown** | Guest Blog (default) | **Flag the row** so a human confirms the profile before publishing. |

To add a domain: put it in `DOMAIN_PROFILE` in `read_batch.py` and in the table above.

## CTA inference (no CTA column in the batch)

The batch sheet has no CTA URL. Infer the Damco service page from the article's
keywords/title, then verify it returns a live page before using it. Start from this map;
if nothing matches, search `damcogroup.com` for the relevant service and verify, and if
still unsure, mark the row `needs-review` in the manifest rather than guessing a slug.

| Topic signal in keywords/title | CTA URL |
|---|---|
| healthcare app / healthcare software | `https://www.damcogroup.com/healthcare/healthcare-app-development` |
| AI consulting / AI consultancy | `https://www.damcogroup.com/ai-consulting-services` |
| AI integration | `https://www.damcogroup.com/ai-integration-services` |
| AI development / custom AI / AI software | `https://www.damcogroup.com/ai-development-services` |
| application management / AMS / app maintenance | `https://www.damcogroup.com/application-management-services` |
| application modernization | `https://www.damcogroup.com/application-modernization-services` |
| Microsoft Dynamics 365 | `https://www.damcogroup.com/microsoft-dynamics-365-services` |
| Salesforce | confirm with the user (the paid-blog brief used `https://achieva.ai/`) |
| generative AI | `https://www.damcogroup.com/generative-ai-services` |
| AI / ML services (general) | `https://www.damcogroup.com/ai-ml-services` |

These are starting points, not a closed list. Always verify the page is live (Gartner-
style bot blocks aside, a 404 means drop it) and pick the page that best matches the
article's specific service.
