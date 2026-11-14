# Where Forest Guardian could plausibly be used

Researched 2026-11-14 from public sources (mostly NGO, project and news pages; few independent
evaluations). Criteria: high risk of illegal logging and/or poaching, and an existing ranger or
task-force deployment that could act on alerts.

## How it would fit
Forest Guardian is not a replacement for what these sites already run. Acoustic alerts exist
(Rainforest Connection Guardians are deployed in several of these places). The gap it targets is
what happens after an alert:
- **Triage at the station:** filter rain/wind false positives before they reach rangers (a known
  risk: too many false alerts erode ranger trust).
- **Check before sending people:** drone or camera check first. This matters where rangers have
  been threatened, injured or killed.
- **Human approval by message:** a ranger approves on a phone. Photon also supports WhatsApp,
  which fits these regions better than iMessage.
- **Tamper-evident evidence:** signed records and cited case files for prosecution.
- **Low bandwidth:** a 108-byte record fits LoRa or satellite links where there is no mobile network.
- **Integration:** feed decisions into the patrol systems sites already use (SMART, EarthRanger)
  rather than a new dashboard.

## Candidates, strongest fit first

| Site | Threats | Existing deployment | Why it fits |
|---|---|---|---|
| **Maya Biosphere Reserve, Guatemala** | Illegal logging, hunting, cattle ranching, fires; lost ~1/3 of forest cover since 1991 | CONAP rangers with WCS; a 2026 AI acoustic project with satellite alerts; older WCS acoustic devices | Rangers find damage days or weeks late; armed groups and remote sites make blind dispatch dangerous. Satellite-sized records, triage and drone-first checks address exactly this. |
| **Cardamom Mountains, Cambodia** | Logging, charcoal kilns, snaring (7,682 snares removed in Mar–May 2024) | Wildlife Alliance rangers from 16 stations, patrolling day and night; SMART training for rangers | Many stations, so dispatch to the nearest one is concrete; gunshot plus chainsaw classes match the threats. |
| **Leuser Ecosystem, Sumatra, Indonesia** | Illegal logging (298 cases, Jan–Nov 2024, in one area), tiger poaching (top threat per a 2025 study) | SMART patrol teams (12 covering ~60% in 2020–21), wildlife patrol units; RFCx sensors elsewhere in Sumatra | Established patrol data system to plug into; high-value species; logging cases frequent. |
| **Romanian Carpathians** | Illegal logging of old-growth forest; poaching (Făgăraș) | Romsilva forest rangers; RFCx/Vodafone "Smart Forest" deployments; Făgăraș anti-poaching with Guardians | Ranger safety: 185 hurt since 2014, two killed within a month. Drone-before-dispatch and court-ready signed evidence matter in an EU legal system. |
| **Alto Rio Guamá Indigenous Territory (Tembé), Pará, Brazil** | Illegal logging, mining, ranching; >30% deforested | ~30 young Tembé guardians; RFCx Guardians since 2014 | Small team, large border: triage and prioritisation stretch a few rangers further. |
| **Chiquibul Forest, Belize** | Cross-border logging (warnings of a comeback), poaching, gold mining, cattle ranching | FCD rangers, joint weekly patrols with the Belize Defence Force and police | Very few rangers (a factsheet says nine) for a large border; joint task-force patrols already exist. Logging is lower than at other sites, so a weaker fit. |
| **EarthRanger sites in Mozambique, Botswana, Republic of Congo** | Poaching | GEF project (2022) deploying EarthRanger across 4.9 M ha with African Parks, AI2 and Conservation International | Not a site pick so much as an integration path: EarthRanger already accepts sensor alerts and notifies the nearest ranger. |

## Honest caveats
- Acoustic logging detection already exists at several of these sites (RFCx). The pitch is the
  station-side triage, evidence and approval layer, not the sensor.
- Effectiveness data for acoustic systems is mostly from the organisations themselves; there is
  little published independent evaluation.
- The classifier was trained on public clips (ESC-50, UrbanSound8K), not field audio from these
  forests; any pilot needs local recordings.
- Several figures above are from 2020–2024; check current numbers before quoting them.

## Sources
- Rainforest Connection Guardian: https://rfcx.org/guardian
- Amazon Conservation (Los Amigos, Peru): https://www.amazonconservation.org/new-technology-helps-find-and-stop-deforestation-using-sound/
- Mongabay 2026, Guatemala AI acoustic monitoring: https://news.mongabay.com/2026/04/in-guatemala-new-ai-technology-will-be-listening-for-illegal-deforestation/
- Context News, Maya Biosphere Reserve threats: https://www.context.news/?p=96357
- InSight Crime, Guatemala–Mexico border patrol: https://insightcrime.org/investigations/jungle-patrol-fighting-illegal-loggers-guatemala-mexico-border/
- Wildlife Alliance / GlobalGiving ranger reports: https://www.globalgiving.org/projects/protectwildlife/
- Global Conservation, Cardamom 2022 report: https://globalconservation.org/news/cardamom-national-park-2022-progress-report
- Mongabay 2025, Sumatran tiger patrols study: https://news.mongabay.com/2025/05/sumatran-tiger-protection-needs-more-patrols-tougher-penalties-study-finds
- Global Conservation, Bengkung Trumon: https://globalconservation.org/news/protection-of-proposed-bengkung-trumon-megafauna-sanctuary
- Global Conservation, Leuser 2020–21: https://globalconservation.org/news/leuser-ecosystem-progress-report-2020-2021
- Romania Insider, Vodafone Smart Forest: https://www.romania-insider.com/smart-forest-romania-vodafone-2021
- Mossy Earth, Făgăraș anti-poaching: https://mossy.earth/projects/rewilding/combating-poaching
- Malay Mail 2023, Carpathian loggers and rangers: https://www.malaymail.com/news/life/2023/08/05/the-fight-to-save-sacred-carpathian-forests-from-loggers/83630
- RFCx Tembé project: https://rfcx.org/projects/tembe
- Google, Tembé story: https://about.google/stories/rainforest/
- FCD Belize protection: https://fcdbelize.org/protection/
- FAO FLEGT, Guatemala–Belize transboundary logging: https://www.fao.org/in-action/eu-fao-flegt-programme/from-the-field/stories-details/es/c/414069/
- GEF, EarthRanger project: https://thegef.org/newsroom/press-releases/new-project-deploys-earthranger-strengthen-protected-areas-africa
