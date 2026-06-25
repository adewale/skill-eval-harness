---
name: swiss-poster
description: Apply a Swiss Poster design system using Tailwind CSS. Use when asked to style a webpage with poster-style layouts where one dominant graphic event breaks the grid, overlaps, bleeds/crops at the edge, uses microtype, hard figure/ground contrast, and extreme typographic scale. Implements IBM Plex Sans, stone color palette, opacity hierarchy, and compositional techniques from Weingart, Troxler, Hofmann, and Odermatt & Tissi.
license: MIT
compatibility: Agent Skills clients including Codex, OpenCode, Pi, Gemini CLI, and Claude Code.
metadata:
  author: adewale
  version: "2.1.0"
---

# Swiss Poster Design System

A design system based on Swiss poster design from the 1950s through 1980s: grotesque type at extreme scales, layouts that break the grid, overlapping layers, diagonal energy, and cropped forms. Draws on Weingart, Troxler, Odermatt & Tissi, Hofmann, and Ruder.

## Output contract for web/UI tasks

When the user asks for a page, component, landing page, or Tailwind/UI styling, return implementable HTML/Tailwind/CSS. Do **not** satisfy a web/UI task with a standalone SVG poster unless the user explicitly asks for an SVG, print asset, or static poster file.

Hard gates before final output:

- Start from an explicit `grid grid-cols-12` structure, then let selected elements escape it.
- Use exactly one accent hue per project (default `#C8102E`). Opacity variants of that hue are allowed; extra accent hues, purple SaaS gradients, and multi-accent palettes are not.
- Make scale contrast extreme: pair viewport-scale `text-[clamp(...)]` display type with `text-[11px]` labels; target 10×+ size ratio and `leading-[0.85]`, tighter custom leading, or `leading-none` at display scale.
- Prevent horizontal scroll. Any section with bleed, negative margins, absolute breakouts, wide `vw` elements, or cropped type needs `overflow-hidden`, `overflow-x-hidden`, or `overflow-clip` on the wrapper.
- Check the 320px boundary: default to `col-span-12`, fluid `clamp()` type, reduced offsets (`md:` and up for aggressive negative margins), no fixed-width elements that force sideways scroll, and 44px minimum touch targets.
- For dense poster HTML, annotate the reading contract: put `data-critical="title"`, `data-critical="body"`, `data-critical="date"`, or `data-critical="cta"` on essential text, and keep decorative anchors `aria-hidden="true"`/`pointer-events-none`.
- For historically grounded, source-heavy, image-heavy, or diagrammatic posters, leave non-visible implementation evidence in markup: `data-reference` for the embodied historical move, `data-source`/`data-beat` for source-ledger items, and `data-encoding`/`data-variable` for real diagram mappings. These attributes are for auditability; do not show eval/provenance labels as visible poster copy.
- Respect non-design boundaries: do not apply this skill to Switzerland geography, tax/legal residency, Swiss German translation, or other non-poster meanings of “Swiss.” If this skill is loaded anyway for a non-design Swiss query, explicitly note that Swiss Poster design does not apply and answer normally only if appropriate. If the user asks for clean corporate Swiss minimalism and explicitly rejects poster drama, restrain the composition instead of forcing this style.

## Dramatic poster recipe

The inspiration set at `https://swiss.ziki.boo/inspiration` shows that drama usually comes from **one dominant graphic event**, not from sprinkling many effects. For each hero or major poster section, choose the event first, then code around it.

**Communication before spectacle.** Historic Swiss posters were public communication: legible from the street, fast to parse, and memorable because the big form carried the message. Drama fails if the title, date, core offer, or CTA becomes hard to read. Use collisions and crops on the anchor or secondary layers; keep the primary reading path clear.

### Protected reading zone heuristic

Historic Swiss poster drama separates **text as image** from **text for reading**. Müller-Brockmann's arcs, Hofmann's figure/ground forms, Matter's photomontage, and Weingart's typographic disruption can be severe because the event name, place, date, or public instruction still has a protected path. The poster may attack the grid; it must not attack comprehension.

Before placing any dramatic anchor, classify layers:

| Layer | Purpose | May be cropped/obscured? | Implementation rule |
| --- | --- | --- | --- |
| **Critical reading** | title, date/time, CTA, safety warning, core claim, essential explanation | No | Put on a quiet field, highest content z-index (`relative z-30`/`z-40`), readable size/leading, strong local contrast against the actual backing |
| **Support reading** | metadata, labels, coordinates, index numbers, captions | Slightly, if still parseable | Tiny but crisp; keep away from high-noise textures |
| **Graphic image** | cropped word, giant number, route line, object, photo fragment, ring, hazard shape | Yes | `aria-hidden`, `pointer-events-none`, lower z-index (`z-0`/`z-10`/`z-20`), opacity/clip/crop allowed |

Use the **quiet-field test**: every critical text block needs an uninterrupted rectangle of calm tone behind it. If a route, slash, ring, photo edge, ghost word, or giant letter crosses that rectangle, one of these must happen:

1. move/crop the graphic so it misses the rectangle;
2. send the graphic behind the text at low opacity (`text-stone-900/[0.06]`, `opacity-10`) so it becomes texture;
3. put the text on a solid light/dark/accent field with padding and higher z-index;
4. invert deliberately with `mix-blend-difference` only after checking the whole word remains readable.

Hard rule: **body copy and CTAs never sit directly on top of high-contrast mega-type, route lines, diagrams, or photos.** Titles may collide only when the collision is the concept and the complete title remains readable in three seconds. Do a final street test: at a glance, can someone read the title, the key fact/date, and the action before admiring the geometry?

### Contrast-channel discipline

Use WCAG/similar accessibility work only as measurement physics, not as aesthetic law. Extract three lessons: contrast is local to the pixels/field behind the text; two high-contrast signals in the same rectangle mask each other; critical information needs first claim on the reader's attention. Do **not** import the baggage: no bland compliance look, no ban on tiny microtype, no ban on crops/overlap/inversion, and no assumption that every layer must be equally readable.

**Do not lower the poster's contrast. Allocate it.** Swiss poster drama should stay brutal: black/stone fields, hard orange/red slabs, severed routes, giant glyphs, and violent figure/ground are allowed. The rule is that maximum contrast cannot be unresolved competition.

Use three channels:

| Channel | What it does | Contrast rule |
| --- | --- | --- |
| **Critical signal** | title, date, CTA, core body/safety claim | Dominant contrast event inside its reading zone; no competing line/photo/glyph through the glyphs |
| **Dramatic signal** | photo, route slash, giant numeral, object, diagram, cropped word | May be maximum contrast, but must route around, sit behind, or become the field for critical text |
| **Texture signal** | halftone, grid, contour, ghost type, dense labels | Can pass behind copy only when demoted: low opacity, low spatial frequency, or tone-matched |

If critical text and a dramatic anchor share a zone, make one of these explicit moves:

- **cut silence:** carve a hard stone-50/stone-950/accent rectangle through the graphic and put the copy there;
- **route around:** bend/stop the route line, slash, or diagram before it enters the reading zone;
- **send behind:** keep the graphic but demote it to texture (`opacity-5`, `opacity-10`, or `text-stone-900/[0.06]`);
- **make the field the drama:** put black text on an orange slab, white text on a black slab, or stone text on a white cutout so the reading zone is itself a graphic event;
- **separate z-indexes:** critical text `relative z-40`, dramatic image `aria-hidden="true" pointer-events-none z-0/z-10`, texture below both.

Never solve readability by making the whole poster polite. Solve it by assigning contrast channels so the title/date/body/CTA win locally while the rest of the field stays severe.

### Artifact fidelity before style

A Swiss poster is an edited public artifact, not a style transfer skin. Before composing, build a private source ledger and make the poster answer it.

| Failure to avoid | Historically grounded correction | Implementation carrier |
| --- | --- | --- |
| Generic slogans replacing real source content | International Style communication discipline: objective facts, names, dates, places, sequence, hierarchy | Preserve required entities; mark dense items with `data-source` or `data-beat`; never invent missing names/talks |
| Photos used as wallpaper texture | Herbert Matter photomontage: image fragments are subject/place/evidence/metaphor | Give each image a role (`data-image-role="place"`, `"person"`, `"evidence"`, `"sequence"`); crop around the role, not randomly |
| Fake diagrams/arrows/nodes | Müller-Brockmann/Ruder systems thinking: marks encode rhythm, order, relation, or measured structure | Add `data-encoding="source variable → visual variable"`; remove any node/line/axis that does not encode a fact |
| Microtype as lorem-text noise | Ruder typographic function: small type still communicates | Microtype must be credits, source notes, coordinates, schedule, index, or captions — not fake texture |
| One-size portrait template | Grid as method, not template | Match the requested/baseline artifact: poster, one-page recap, landscape, thread sequence, matrix, map, or specimen |
| Prompt-shaped artifacts | Public poster copy, not implementation notes | Never print dimensions, hex codes, “single accent,” “current skill,” “source material,” “brief,” “generated,” or eval/provenance labels |
| Clipped mandatory content | Public-viewing discipline | Essential ledger items must remain inside the canvas and unobscured; crop only graphic/support layers |

If a prompt provides tweets, images, historic facts, or a baseline artifact, preserve its information structure before applying style: count the beats, keep real names/places/dates, choose which items are title/support/microtype, and only then decide the visual anchor. If you cannot verify an exact source fact, do not hallucinate specificity; use a broader true label or state the missing input outside the artwork.

### Historical period and genre selection

Do not accidentally anchor every poster in late-1960s/1980s black-white-red typographic disruption. Swiss poster history is broader than Weingart/Troxler energy. Pick a lineage and genre because it fits the subject, then let that choice constrain color, image handling, typography, grid behavior, and material surface.

| Lineage / period | Best fit | Use | Avoid |
| --- | --- | --- | --- |
| **Sachplakat / object poster** (1900s–1930s influence) | Products, artifacts, tools, food, single technical object | one isolated object/silhouette, near-clinical product facts, quiet background, direct name | giant type violence, fake diagrams |
| **Travel / photomontage / lithographic poster** (1930s–1950s) | places, journeys, civic destinations, image stories | saturated but disciplined color, cropped photographs/illustration, overprint, paper/grain, place as hero | treating photos as wallpaper or UI cards |
| **Zurich International Typographic Style** (1950s–1960s) | public notices, schedules, institutions, education | restrained asymmetric grid, clear hierarchy, objective facts, small number of rules | expressive chaos or decorative microtype |
| **Basel / Hofmann figure-ground** (1950s–1960s) | theatre, civic warning, conceptual tension | severe black/white mass, point-line-plane, 2–3 contrast axes, quiet drama | clutter, many simultaneous effects |
| **Geigy / scientific systems** (1950s–1970s) | science, pharma, data, protocols, taxonomies | specimen/matrix/plate, measured labels, data-to-form mapping, restrained color | generic route diagrams or SaaS dashboard panels |
| **Matter-style photomontage** | editorial/photo-rich source material | authored crop, scale juxtaposition, duotone/overprint, captions with roles | full-bleed photo as background texture |
| **Weingart / New Wave typography** (1970s–1980s) | typography, publishing, rebellion, fractured systems | one controlled rupture, type as image, offset layers with hierarchy intact | using chaotic type for every subject |
| **Troxler / performance rhythm** | music, festivals, live performance | optical rhythm, repeated marks, surprise, pulse, event-specific energy | same line/ring field on non-musical subjects |
| **Contemporary Swiss cultural poster** | modern cultural/tech hybrids | conceptual image, expressive but source-specific system, print-process awareness | generic web-brutalist poster kit |

Use non-visible audit markers when helpful: `data-lineage="geigy-scientific"`, `data-period="1950s-zurich"`, `data-genre="object-poster"`, `data-print-process="lithographic-overprint"`, `data-contrast="large-small"`. These must reflect real layout decisions. Do not show the markers as poster copy.

Specific anti-collapse rules:

- **Palette breadth:** one accent still applies, but it need not be red/orange. Travel may choose lake blue, alpine green, ochre, or sky cyan; scientific posters may use muted analytical color; theatre may use black/white with a restrained accent.
- **Typographic violence budget:** cropped mega-type is one lineage, not the default. Object, travel, scientific, and Zurich-style posters may use quieter type if image/object/data is the anchor.
- **Grid invisibility:** `grid-cols-12` is construction scaffolding; the final poster should feel proportioned, not like a web dashboard. Avoid rounded cards, CTA modules, feature panels, and browser UI patterns unless the subject is explicitly UI.
- **Material process:** choose a print surface when relevant — overprint, halftone, duotone, misregistration, ink trap, lithographic grain, screenprint slab, paper tone — without making texture obscure critical copy.
- **Diagram restraint:** diagrams are not a default Swiss motif. Use them only for systems/data subjects; theatre, travel, product, and manifesto prompts often need image, object, or figure-ground instead.
- **Restraint as drama:** Hofmann/Ruder restraint can be more Swiss than maximal collision. Choose 2–3 contrast axes and suppress unrelated effects.
- **Genre breadth:** ask whether the poster is a travel placard, object poster, scientific plate, public notice, concert poster, theatre sheet, typographic specimen, editorial photomontage, or contemporary cultural poster before coding.

### Rendered poster proof, not metadata compliance

Invisible attributes are audit hooks, not achievement. A poster that says `data-lineage="geigy-scientific"` but looks like a Cloudflare event diagram has failed. The rendered image must prove the claim.

Before final output, do a visual proof pass:

- **Visual grammar proof:** if the claimed lineage is travel, object, Geigy/scientific, theatre, or photomontage, the dominant form must visibly match that lineage without reading attributes.
- **Readable-at-image proof:** title, date/key fact, CTA/body must be visible in the screenshot, inside the canvas, not hidden by crop, and not dependent on zoom.
- **Source-support readability:** source-bearing support copy is not exempt just because it is smaller than the headline. If a line preserves a real fact such as “run locally via CLI,” every word must remain readable at image scale or the fact should move to a protected/quiet field.
- **No split-field text loss:** critical text must not straddle a light/dark or image/field boundary unless each side deliberately flips color or the whole line sits on a solid backing. `z-index` does not fix black text crossing a black field.
- **Source-fidelity proof:** the visible poster preserves required names, dates, places, sequence beats, source images, or measured facts; attributes cannot compensate for missing public content.
- **Real poster proof:** the composition should read as a print artifact at thumbnail and full scale, not as a compliant HTML layout with labels.

### Avoid UI/product language unless the subject is UI

Most posters are not landing pages. Avoid reflexive web-language artifacts:

- CTA-button stacks, feature cards, dashboard panels, pricing-table rows, “learn more” modules, and product-marketing blocks.
- Labels like “recap,” “index,” “fig,” “system,” “channel,” or “route” when they are not real source content.
- Rounded cards, soft shadows, button groups, nav-like headers, SaaS feature matrices, and app-dashboard grids.

A call to action can exist, but it should feel like a poster instruction (`Reserve seats`, `Book the night train`, `Vote Sunday`, `Bring a demo`) rather than a web button module unless the task is explicitly a UI.

Source websites often contain navigation/button copy (`View documentation`, `Get started`, `Read the docs`, `Open GitHub`). Do not automatically promote those labels into the poster CTA. Treat them as web chrome unless the user explicitly asks for a web page. For a print/poster artifact, either omit the CTA or derive a poster-native instruction from the subject and source facts.

### Typography lineage matters

Swiss poster typography is not one Helvetica-like voice. IBM Plex Sans is a practical open fallback for modern grotesk work, not a history eraser and not the default answer for every era. Choose a typographic stance by lineage before choosing a font file:

| Lineage | Typographic stance | Open implementation proxy |
| --- | --- | --- |
| Sachplakat / object poster | sparse direct product name, custom/commercial wordmark, fewer sizes; object carries the drama | IBM Plex Sans or Barlow Condensed for labels; hand-built/CSS-treated display word |
| Travel / lithographic | destination lettering may be condensed, painted, serifed, or poster-lettered; image/color carries more than type violence | Barlow Condensed, IBM Plex Serif, Georgia, or custom letter spacing/outline treatment |
| Photomontage / Matter | type integrates with image crops through scale, diagonal placement, overprint, and captions | grotesk or condensed sans plus authored photo/type masks |
| Zurich / International Style | neo-grotesk rationalism, clean weights, tight hierarchy, Helvetica/Neue Haas/Univers/Akzidenz-like proportions | IBM Plex Sans, Hanken Grotesk, Helvetica Neue/Arial/system-ui |
| Basel / Hofmann-Ruder | restrained grotesk, strong spacing, severe hierarchy, generous negative space | IBM Plex Sans/Hanken Grotesk with careful tracking and fewer weights |
| Geigy / scientific | narrow grotesk/mono/specimen labels, measured tables, controlled captions, technical plates | Barlow/Barlow Condensed, IBM Plex Mono, small caps/tracked labels |
| Weingart / New Wave | disrupted spacing, fragments, halftone/type-as-image; only when subject warrants rupture | grotesk fragments, extreme tracking, transforms, layered copies |
| Troxler / contemporary cultural | expressive rhythm, illustration/type fusion, event-specific display lettering | custom CSS lettering, condensed/display sans, or image-led type treatment |

Treat commercial historical names as references, not required web fonts. Use system fallbacks where necessary, but vary family category, width, case, spacing, and role so every poster does not feel like the same IBM Plex tech artifact. If the subject predates or rejects International Style, do not force a clean neo-grotesk grid merely because it looks “Swiss.”

### Color discipline can be richer than one red accent

The default one-accent rule is anti-slop, not anti-history. Use strict one-accent when the subject has no better palette or when the output risks AI gradient slop. But some Swiss lineages need disciplined multi-tone systems:

- travel/lithographic posters may use 2–3 flat inks plus paper tone;
- scientific/Geigy plates may use muted analytical colors to separate variables;
- civic/political posters may use party/signal colors when source-grounded;
- photomontage may use duotone/overprint pairs.

If using more than one hue, make it a **limited print palette** (`data-palette="three-flat-inks"`), not a gradient rainbow. Every hue must have a role: place, variable, warning, party, route, or ink overprint.

### Image authorship

Do not merely place images. Author them like poster material:

- crop around the meaningful subject, not the center of the file;
- use scale juxtaposition, angle, mask, duotone, overprint, or halftone;
- caption or index images when they carry source evidence;
- make one image fragment the dominant argument when the prompt is photo-led;
- avoid rounded thumbnails unless the brief is explicitly UI.

### Academic literature cautions

Design-history scholarship treats “Swiss Graphic Design” as a contested national label, not a single style. Use that humility in the artwork:

- **Do not collapse the canon.** Müller-Brockmann, Hofmann, Ruder, Matter, Weingart, Troxler, Geigy, tourism, object posters, schools, printers, clients, awards, and collectors are different histories, not interchangeable Swiss garnish.
- **Public infrastructure matters.** Swiss posters were shaped by fixed formats, billposting systems, printers, exhibition furniture, public reading distance, and city space. The result should feel like public display matter, not a landing page.
- **Client and institution matter.** A poster is an interface between a source and a public: event, venue, client, sponsor, route, product, institution, and date are material, not filler.
- **School labels are shortcuts.** “Basel,” “Zurich,” and “Swiss Style” simplify messy institutions and practices. Use them only to select concrete methods; never print them as proof.
- **Typography is production history.** Letterpress, lithography, photomontage, phototypesetting, and digital type imply different type behavior. Choose type by era/process/subject.
- **Scripts are not texture.** Non-Latin or unfamiliar writing systems must be meaningful, accurate, and readable; never use them as exotic visual noise.

### Historical grounding is embodied, not named

Designer names are not decoration. A visible label saying “Weingart / Hofmann / Matter” does not make a poster historically grounded. Translate the reference into a concrete compositional move:

| Historical source | Embodied move | Modern carrier | Common fake version |
| --- | --- | --- | --- |
| Josef Müller-Brockmann / concert posters | Rhythm made visible through arcs, repetition, score-like intervals, disciplined public info | `data-reference="muller-brockmann-rhythm"`, concentric/radial systems, repeated rules, strict metadata path | Random rings behind generic title |
| Armin Hofmann / Basel figure-ground | Severe black/white mass, point-line-plane tension, image/shape as subject | `data-reference="hofmann-figure-ground"`, hard fields, geometric abstraction, deliberate negative space | Bland card with black rectangle |
| Herbert Matter / photomontage | Cropped image fragments, scale juxtaposition, overprint, place/action as image | `data-reference="matter-photomontage"`, masked/cropped assets with captions/roles | Full-bleed photo used as wallpaper |
| Emil Ruder / typography | Type hierarchy, spacing, legibility, small type with function | `data-reference="ruder-typographic-function"`, real captions, measured line length, disciplined microtype | Tiny decorative pseudo-data |
| Wolfgang Weingart | Disrupted type that still has hierarchy | `data-reference="weingart-controlled-disruption"`, one typographic rupture, offset columns, rule breaks with readable path | Chaotic overlaps everywhere |
| Niklaus Troxler | Music/performance as rhythm, optical vibration, poster-specific surprise | `data-reference="troxler-rhythm"`, repeated marks/pulses tied to event energy | Same line field on every subject |
| Odermatt & Tissi | Bold commercial clarity, edge pressure, witty type/image relation | `data-reference="odermatt-tissi-commercial-clarity"`, direct offer, massive product/name, sharp asymmetric crop | Generic Swiss corporate minimalism |

Use one historical move, not a museum collage. The move must fit the subject and change the layout. If the designer/reference names were removed, the historical logic should still be visible.

### Choose by subject, not by default

Do **not** reuse the same “dark right field + red bar + cropped word + rings/lines” recipe for every prompt. Pick the archetype whose visual logic comes from the subject:

| Subject / message | Better semantic archetype | Avoid |
| --- | --- | --- |
| Product, object, artifact | Object poster: isolated cutout/silhouette, one extreme crop, tiny product facts | Abstract rings unless the product is circular/rhythmic |
| Transit, logistics, location | Route/map poster: paths, nodes, transfer points, coordinates, directional arrows | Decorative line fields that do not encode routes |
| Report, dashboard, scientific/technical system | Data/diagram poster: axis, matrix, table, bars, schematic, measured labels | Fake chart garnish or generic “big number” |
| Typography, writing, publishing | Typographic specimen: glyph, baseline grid, x-height, letterform crop, type scale | Photo collage or arbitrary dates |
| Travel, editorial, portfolio image story | Photomontage: multiple cropped image fragments, overprint, scale juxtaposition | Rounded image cards or single documentary thumbnail |
| Civic, safety, warning | Scale juxtaposition: oversized hazard vs. small vulnerable figure, clear warning text | Vague abstract geometry that hides the message |
| Music/performance | Rhythm poster: repetition, pulse, syncopation, waves, score-like structure | Giant date unless the date is the point |

The same principle can unfold into many posters: first translate the subject into a visual grammar, then apply grid, scale, crop, microtype, and figure/ground tension.

Every dramatic section needs all five carriers:

1. **Dominant anchor with meaning:** one cropped word, giant numeral/date, oversized circle/ring, hard color field, or cropped photo fragment occupying roughly 35–70% of the first viewport/section. It must relate to the content, not be arbitrary decoration.
2. **Edge pressure:** the anchor is pinned to, sliced by, or bleeding past at least one frame edge. Use `overflow-hidden`, `whitespace-nowrap`, `-ml-[0.06em]`, `-translate-x-*`, `-right-*`, or `clip-path` deliberately.
3. **Microtype/data layer:** 2–4 tiny metadata clusters (`text-[11px] tracking-widest uppercase`) for real dates, index numbers, locations, coordinates, or labels. These make the large move feel intentional.
4. **Hard figure/ground contrast:** large stone-900/stone-50 fields, split backgrounds, `mix-blend-difference`, or accent slabs. Drama can come from black/white mass even when the accent is sparse.
5. **Graphic system:** geometry, repetition, rhythm, or image treatment beyond a decorative rule — concentric rings, translucent circle stacks, diagonal bars, line fields, dot matrices, repeated words, masked photos, or halftone overlays.

Choose one archetype per section:

| Archetype | Use when | Required move |
| --- | --- | --- |
| Cropped-word poster | A concept or product name can become the image | One word at `clamp(6rem,18vw,24rem)`, cropped by an edge, with tiny labels nearby |
| Giant-date/numeral poster | Events, reports, launches, versioned products | Date/number occupies 40%+ of section; content becomes a small grid of metadata |
| Split-field crossover | Need immediate value contrast | Light/dark halves with display type crossing the boundary via `mix-blend-difference` |
| Geometric-anchor poster | Abstract/technical/cultural subject | One oversized circle/ring/bar/triangle is the subject, not ornament |
| Masked-photo poster | A photo is available | Photo is high-contrast, grayscale/duotone, cropped as a shape, overprinted with type/fields |
| Rhythm/texture poster | Need motion or music/data energy | Repeating lines/dots/words create optical vibration behind restrained copy |
| Stacked-collage poster | Dense story or multiple assets | Hard-edged layers overlap with explicit `z-10/z-20/z-30` and no soft shadows |

Avoid the timid middle: a centered heading, a paragraph, three rounded cards, and a small accent line is Swiss-flavored web minimalism, not a dramatic poster. Avoid the opposite failure too: unreadable pileups, random giant dates, decorative microtype that does not communicate, or repeating the same motif because it worked once.

## Six Principles

1. **Grid as launchpad.** Start with a 12-column grid, then let key elements escape it. Oversized type, images, and color blocks should break column boundaries, overlap neighbors, or bleed off the viewport edge. The grid exists so the breakout has meaning.
2. **Extreme scale contrast.** Place 20rem display type next to 11px labels. A single word can fill the viewport width. The tension between massive and tiny *is* the hierarchy. Never settle for moderate size differences.
3. **Overlap and layer.** Elements should collide — text over images, type over type, color blocks overlapping content. Use `relative`/`absolute` positioning, negative margins, and z-index stacking to create depth. Collision belongs to graphic/support layers; critical reading text gets a protected quiet field and higher z-index.
4. **Bleed and crop.** Let elements escape their containers. Type cropped by the viewport edge, images that extend past the layout, color blocks that run off-screen. A composition that's cut off implies it continues beyond the frame.
5. **One accent, used boldly.** Each project gets exactly one accent color, but where the International Style uses it sparingly, poster style uses it in large, confident fields — full-width bands, oversized shapes, dramatic backgrounds.
6. **Tension over comfort.** Asymmetric whitespace, unexpected element placement, rotated text, diagonal compositions. The layout should feel dynamic and slightly unresolved, not settled and safe.

---

## Anti-Slop

Adapted from **Impeccable** — <https://impeccable.style/slop/>. "Slop" is the look of an interface no one decided on: the reflexive default chosen because it was easy. This system is slop-resistant by construction, but the defaults still tempt. **Every gradient, glow, blur, rotation, and font must be a choice you could defend.** If you can't say why it's there, delete it.

Never ship these reflexively:

- **No AI palette.** No purple/violet gradients, no cyan-on-black. Stone + **one** accent only.
- **No gradient text, no reflexive glow.** Flat fills. A glow is allowed only when it is the poster's actual subject, never as polish.
- **No pure `#000`/`#fff`.** Use `stone-950` / `stone-50`.
- **No reflexive font monoculture.** Use an approved lineage proxy: neo-grotesk, condensed, serif/display, mono/technical, or custom lettering. Avoid Inter, Roboto, Geist, Space Grotesk, Plus Jakarta Sans, and the same IBM Plex Sans treatment on every poster.
- **Monospace must earn its place.** Mono is for code, data, IDs, and metadata — not decoration.
- **No card slop.** No identical icon-tile feature-card grids, cards-in-cards, pill buttons, soft shadows, or accent stripes taped to one side of a box.
- **No prompt/provenance leakage as visible copy.** Do not print dimensions (`840 × 1200`), hex codes, “single accent,” “current skill,” “source material,” “brief,” “generated,” “prompt,” “audit,” oracle/eval labels, or implementation instructions unless the user explicitly asked for process documentation outside the artwork.
- **Not everything centered.** Asymmetric, flush-left body. It is still a bug if it fails AA contrast, has cramped padding, or is justified.

Verify with:

```sh
npx impeccable detect <file-or-url>
```

---

## Typography

**Font strategy:** choose a family category for the lineage, then use available web/system fonts as proxies. IBM Plex Sans remains the safe open neo-grotesk fallback; it is not the universal Swiss poster voice.

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700&family=Barlow:wght@400;500;700&family=Hanken+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:ital,wght@0,100;0,300;0,400;0,500;0,600;0,700;1,300;1,400&family=IBM+Plex+Serif:wght@400;600;700&display=swap" rel="stylesheet">
```

**Approved implementation families by role:**

| Role | Font category | Good open/system proxies | Use when |
| ---- | ------------- | ------------------------ | -------- |
| Neo-grotesk sans | rational grotesk | IBM Plex Sans, Hanken Grotesk, Helvetica Neue, Arial, system-ui | Zurich/Basel grid, public notices, institutions |
| Condensed sans | narrow/display grotesk | Barlow Condensed, Barlow | travel labels, object posters, Geigy labels, compressed metadata |
| Serif/display | editorial or lithographic proxy | IBM Plex Serif, Georgia | travel/lithographic or historic placards when image/color leads |
| Mono/technical | measured labels/code/data | IBM Plex Mono, ui-monospace | scientific plates, code/framework posters, axes, source ledgers |
| Custom lettering | treated word/image rather than font choice | CSS transforms, outlines, masks, extreme tracking, hand-built SVG/text shapes only when appropriate | Sachplakat wordmarks, Troxler/contemporary cultural posters, New Wave fragments |

Do not use a broader font palette as decoration. The font category must follow the poster’s era, subject, and image grammar.

**Type scale — poster range:**

The poster style uses a wider range of sizes than the International Style, and the gaps between levels are deliberately extreme.

| Role | Tailwind | Weight | Line height | Notes |
| ---- | -------- | ------ | ----------- | ----- |
| Mega | `text-[clamp(6rem,15vw,20rem)]` | `font-bold` or `font-thin` | `leading-[0.85]` | Viewport-scale type. One word or short phrase. |
| Display | `text-7xl md:text-8xl lg:text-9xl` | `font-light` or `font-bold` | `leading-none` | Section anchors |
| H1 | `text-5xl md:text-6xl` | `font-light` | `leading-tight` | Page titles |
| H2 | `text-4xl` | `font-light tracking-tight` | `leading-snug` | Section headings |
| H3 | `text-2xl` | `font-normal` | `leading-snug` | Subsections |
| Body | `text-base font-normal` | `font-normal` | `leading-relaxed` | `max-w-[60ch]` |
| Small | `text-sm font-normal` | `font-normal` | `leading-relaxed` | `max-w-[60ch]` |
| Caption | `text-xs tracking-widest uppercase` | `font-normal` | `leading-normal` | Labels, metadata |
| Mono | `font-mono text-sm` | `font-normal` | `leading-relaxed` | Code, data |
| Label | `text-[11px] tracking-widest uppercase` | `font-medium` | `leading-none` | Smallest text |

**Key differences from International Style, when the lineage calls for type-as-image:**
- **Bold or thin display type is allowed.** `font-bold` and `font-thin` (100) can create dramatic weight contrast, but object, travel, scientific, and Ruder/Hofmann restraint may need quieter weights.
- **Extreme size jumps are one tool, not a universal law.** A 20rem heading next to 11px labels is right for cropped-word/New-Wave/event posters; object/travel/scientific posters may let image, object, or diagram be the anchor.
- **Tight leading at large sizes.** Display type can use `leading-[0.85]` or `leading-none`; do not let tight type obscure public information.
- **Type as image is lineage-specific.** At poster scale, letterforms can become graphic elements, but not every Swiss poster should become a giant IBM Plex word crop.

---

## Color System

### Stone palette (light mode → dark mode)

| Role | Light | Dark | Tailwind |
| ---- | ----- | ---- | -------- |
| Page background | `stone-50` | `stone-950` | `bg-stone-50 dark:bg-stone-950` |
| Surface / card | `stone-100` | `stone-900` | `bg-stone-100 dark:bg-stone-900` |
| Subtle surface | `stone-200` | `stone-800` | `bg-stone-200 dark:bg-stone-800` |
| Border | `stone-200` | `stone-800` | `border-stone-200 dark:border-stone-800` |
| Primary text | `stone-900` | `stone-50` | `text-stone-900 dark:text-stone-50` |
| Secondary text | `stone-900/70` | `stone-50/70` | `text-stone-900/70 dark:text-stone-50/70` |
| Tertiary text | `stone-900/40` | `stone-50/40` | `text-stone-900/40 dark:text-stone-50/40` |

### Opacity hierarchy

To de-emphasize text, reduce opacity — never change the hue.

```
Full presence:   text-stone-900          (primary)
Softer:          text-stone-900/70       (secondary, labels)
Quiet:           text-stone-900/40       (tertiary, captions)
Ghosted:         text-stone-900/20       (disabled, placeholder)
```

Dark mode: replace `stone-900` with `stone-50`. The opacity values stay identical.

### Accent color

Each project uses **one** accent color. Default is Swiss poster red.

| Name | Hex | Tailwind arbitrary |
| ---- | --- | ------------------ |
| Swiss Red (default) | `#C8102E` | `[#C8102E]` |
| Cobalt | `#003B8E` | `[#003B8E]` |
| Golden | `#F0B429` | `[#F0B429]` |
| Forest | `#2D6A4F` | `[#2D6A4F]` |

**Poster-style accent usage — bolder than International Style:**

```
Full field:  bg-[#C8102E]                Full-width bands, large shapes, hero backgrounds
Overlay:     bg-[#C8102E]/80            Translucent overlay on images or sections
Muted:       bg-[#C8102E]/60            Hover states, secondary elements
Tint:        bg-[#C8102E]/20            Background washes, card tints
Ghost:       bg-[#C8102E]/10            Very subtle tints
```

The poster style permits large accent surfaces. A full-width red band, a half-page accent block, or accent type at mega scale are all correct. The International Style limited accent to 10–15% of visual surface; poster style can push to 30–40% when the composition demands it.

---

## Grid & Breakout

**Base unit:** 8px. Spacing is multiples of 8, but elements are free to escape the grid.

### The grid is a starting point

```html
<!-- Standard 12-column grid — the foundation -->
<div class="grid grid-cols-12 gap-4 md:gap-8">
  <div class="col-span-12 md:col-span-8">...</div>
  <div class="col-span-12 md:col-span-4">...</div>
</div>
```

### Breaking the grid (the goal)

Elements escape their grid cells using negative margins, absolute positioning, and overflow:

```html
<!-- Type that bleeds left out of its container -->
<div class="max-w-6xl mx-auto px-8 relative">
  <h1 class="text-[clamp(6rem,15vw,20rem)] font-bold leading-[0.85] -ml-[0.04em] text-stone-900 dark:text-stone-50">
    PLAKAT
  </h1>
</div>

<!-- Element that escapes its column into the neighbor -->
<div class="grid grid-cols-12 gap-8">
  <div class="col-span-5 relative">
    <div class="absolute -right-24 top-0 w-64 h-64 bg-[#C8102E]"></div>
  </div>
  <div class="col-span-7">...</div>
</div>

<!-- Full-bleed accent band that ignores the container -->
<div class="bg-[#C8102E] -mx-[100vw] px-[100vw] py-16">
  <div class="max-w-6xl mx-auto">...</div>
</div>
```

### Overlap patterns

```html
<!-- Text overlapping a color block -->
<div class="relative">
  <div class="w-2/3 h-64 bg-[#C8102E]"></div>
  <h2 class="absolute -bottom-8 right-0 text-7xl font-bold text-stone-900 dark:text-stone-50">
    Gestaltung
  </h2>
</div>

<!-- Stacked layers with offset -->
<div class="relative">
  <div class="bg-stone-200 dark:bg-stone-800 p-12 ml-16 mt-16">Content</div>
  <div class="absolute top-0 left-0 w-32 h-32 bg-[#C8102E]"></div>
</div>
```

---

## Image, Geometry & Texture

Images and shapes must behave like poster subjects, not decorative assets.

```html
<!-- Cropped high-contrast photo block with overprint -->
<section class="relative overflow-hidden bg-stone-50 text-stone-900">
  <div class="grid grid-cols-12 gap-4 md:gap-8 min-h-[80svh]">
    <div class="col-span-12 md:col-span-7 relative overflow-hidden bg-stone-900">
      <img src="/photo.jpg" alt="" class="absolute inset-0 h-full w-full object-cover grayscale contrast-150 mix-blend-screen opacity-80" />
      <div class="absolute inset-y-0 right-0 w-1/3 bg-[#C8102E]/80 mix-blend-multiply"></div>
    </div>
    <div class="col-span-12 md:col-span-5 relative p-6 md:p-10">
      <p class="text-[11px] tracking-widest uppercase text-stone-900/50">Basel / 1962 / Fig. 04</p>
      <h2 class="mt-8 -ml-[0.08em] text-[clamp(5rem,14vw,16rem)] font-bold leading-[0.8] tracking-tighter">PHOTO</h2>
    </div>
  </div>
</section>

<!-- Repetition layer: line field / dot matrix / concentric rings -->
<div class="absolute inset-0 pointer-events-none text-stone-900/[0.08]"
  style="background-image: repeating-linear-gradient(90deg, currentColor 0 1px, transparent 1px 8px)"></div>
<div class="absolute -right-[20%] -bottom-[30%] h-[70vw] w-[70vw] rounded-full border-[10px] border-[#C8102E]"></div>
```

Use `grayscale`, `contrast-125`/`contrast-150`, `object-cover`, rectangular masks, `mix-blend-multiply`, `mix-blend-screen`, or `mix-blend-difference` to turn photography into graphic material. Avoid rounded image cards and documentary thumbnail grids.

---

## Rotation & Diagonal Energy

Rotated elements create movement and break the horizontal/vertical monotony:

```html
<!-- Rotated section label -->
<span class="text-xs tracking-widest uppercase text-stone-900/40 -rotate-90 origin-left">
  Section 01
</span>

<!-- Diagonal accent bar -->
<div class="w-full h-2 bg-[#C8102E] -rotate-3 scale-110 origin-center"></div>

<!-- Rotated display type as background texture (8–12% opacity minimum) -->
<div class="absolute -rotate-12 text-[12rem] font-bold text-stone-900/[0.10] dark:text-stone-50/[0.10] select-none pointer-events-none">
  POSTER
</div>
```

---

## Responsive Design

Every layout must work at 320px and 1440px. The poster drama scales — it doesn't disappear on mobile.

**Breakpoint strategy:**

| Prefix | Width | Use for |
| ------ | ----- | ------- |
| (none) | 0px+ | Mobile — single column, but still dramatic scale |
| `sm:` | 640px+ | Large phones — overlaps begin to appear |
| `md:` | 768px+ | Tablets — 2-col layouts, full overlap compositions |
| `lg:` | 1024px+ | Desktop — full poster compositions, maximum scale contrast |

**Mobile poster rules:**
- Mega type scales down but stays dominant: `text-5xl sm:text-7xl md:text-8xl lg:text-[12rem]`
- Overlaps simplify: stack vertically, reduce offsets
- Bleeds still work: full-width accent bands remain full-width
- Rotation reduces: `rotate-0 md:-rotate-3`
- Grid breaks simplify: negative margins reduce on small screens

**Fluid type pattern:**

```html
<!-- Poster-scale fluid type -->
<h1 class="text-[clamp(3rem,12vw,16rem)] font-bold leading-[0.85] tracking-tight">
  ZÜRICH
</h1>
```

---

## Dark Mode

Use Tailwind's `media` strategy (respects system preference automatically):

```js
// tailwind.config.js
darkMode: 'media'
```

Every color token has a `dark:` variant. See the stone palette table above. Never use `bg-black` or `bg-white` — always use stone scale.

**Poster sections with inverted backgrounds** are common: a `bg-stone-900 dark:bg-stone-50` section inside a light page creates dramatic contrast without relying on dark mode.

---

## Gotchas

- **The grid must exist before you break it.** Every layout starts with `grid grid-cols-12`. Breakout elements use negative margins, absolute positioning, or overflow to escape — they don't ignore the grid entirely.
- **Scale contrast must be extreme.** If your largest and smallest text are within 3× size difference, you're still in International Style territory. Aim for 10×+ (e.g., 12rem heading / 11px label).
- **Overlap needs a clear layer order.** Every overlapping composition must have a deliberate `z-10`, `z-20`, `z-30` stacking. Random overlap looks like a bug.
- **Bleed needs clipping on the parent.** Escaped elements must not cause horizontal scroll — that is a hard failure, not an acceptable poster effect. Apply `overflow-hidden`, `overflow-x-hidden`, or `overflow-clip` to the section/page wrapper and reduce negative margins on mobile.
- **Bold at display sizes, light at body.** `font-bold` / `font-thin` are for mega/display type only. Body text stays `font-normal`. This is the opposite of the International Style's "never bold" rule.
- **Rotation is seasoning, not the meal.** One or two rotated elements per section maximum, and each must reinforce a chosen axis (vertical edge, diagonal slash, radial anchor). Random rotation feels gimmicky.
- **Accent fields can be large.** Full-width bands, half-page blocks, oversized shapes — poster style embraces large accent surfaces that the International Style would forbid.
- **No border-radius on structural elements.** The poster style is still rectilinear. No `rounded-lg`, no pills. `rounded-none` or at most `rounded-sm`.
- **Never `bg-white` or `bg-black`.** Use `bg-stone-50` / `bg-stone-950`.
- **One accent per project.** Even with bolder usage, the discipline of a single accent remains: one hue plus opacity, not red plus blue plus purple, and no gradient palette masquerading as an accent.
- **No centered SaaS composition.** A centered hero stack plus equal feature cards is a failure unless a dominant cropped anchor or hard field disrupts it.
- **No illegible drama.** The dominant anchor may be cropped, but the primary title, essential metadata, and CTA must remain readable. If overlap reduces comprehension, move the overlap to a secondary layer.
- **No AI tells by reflex.** No purple/cyan gradients, gradient text, decorative glow, overused fonts, soft shadows, rounded card grids, or monospace-as-decoration. Run `npx impeccable detect` to verify.
- **Touch targets minimum 44px.** All interactive elements must remain usable despite the dramatic compositions.
- **Every layout must work on mobile.** Poster drama scales down gracefully. Overlaps simplify, mega type shrinks (but stays dominant), bleeds remain.

---

## When to read reference files

| Task | File |
| ---- | ---- |
| Full color token table, CSS custom properties, dark mode details | `references/design-system.md` |
| Tailwind component patterns: buttons, cards, nav, forms, poster compositions | `references/components.md` |
| Paste-ready `tailwind.config.js` and CSS `@theme` block | `references/tailwind-config.md` |
| Designer research, key works, source URLs | `references/research.md` |
| Applying this system to an existing page, audit checklist | `references/prompting.md` |
