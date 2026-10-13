// Sources shared by the tests and the build-time selftest (which runs in the
// image, where test/ is not copied).

import { readFileSync } from 'node:fs';

// Card #413: the shared rich fixture, an invented deployment diagram using
// every advanced feature agents reach for (front matter config and
// themeVariables, classDef and class, style on subgraphs, nested subgraphs
// with direction, hexagon, cylinder, stadium and subroutine shapes, edge IDs
// with `animate`, dotted and thick labelled edges). The same file is used by
// the renderer's selftest (diagram-renderer/scripts/fixtures/ holds an
// identical copy, compared in CI), the wiki reader test in memory-ui and the
// MCP tool tests. Invented content only: no real hosts, addresses or names.
export const RICH_NETWORK = readFileSync(new URL('./fixtures/rich-network.mmd', import.meta.url), 'utf8');

// Card #412, acceptance 2: front matter config, edge IDs with `animate`,
// `@{ shape: ... }` nodes, classDef and nested subgraphs.
export const RICH_FLOWCHART = `---
title: Request path
config:
  theme: base
  flowchart:
    curve: basis
---
flowchart TD
  classDef hot fill:#f96,stroke:#333
  classDef cold fill:#9cf
  client@{ shape: circle, label: "Client" }
  gw@{ shape: rounded, label: "Gateway" }
  client e1@--> gw
  e1@{ animate: true }
  subgraph cluster [Cluster]
    direction LR
    subgraph app [App]
      api[API]:::hot
      worker[Worker]
    end
    subgraph data [Data]
      db@{ shape: cyl, label: "Postgres" }
    end
  end
  gw e2@==> api
  e2@{ animation: slow }
  api --> worker
  worker -.-> db
  class db cold`;

// Broken sources with the line of the mistake, counted in the source as
// stored. Each has something before the mistake that Mermaid strips before
// it parses, so a raw Mermaid line number would be wrong for all but two.
const B = '  B -->> \n  C[[';
export const BROKEN = [
  ['front matter', '---\ntitle: x\nconfig:\n  theme: base\n---\nflowchart TD\n  A --> B\n' + B, 8],
  ['front matter then blank lines', '---\ntitle: x\n---\n\n\nflowchart TD\n  A --> B\n' + B, 8],
  ['a leading directive', '%%{init: {"theme":"base"}}%%\nflowchart TD\n  A --> B\n' + B, 4],
  ['a directive inside', 'flowchart TD\n%%{init: {"theme":"base"}}%%\n  A --> B\n' + B, 4],
  ['leading comments', '%% hi\n%% there\nflowchart TD\n  A --> B\n' + B, 5],
  ['comments inside', 'flowchart TD\n  A --> B\n  %% c\n\n  %% d\n' + B, 6],
  ['blank lines after a brace', 'flowchart TD\n  A{x}\n\n\n  A --> B\n' + B, 6],
  ['a Langium grammar (pie)', '---\ntitle: x\n---\npie\n  %% c\n  "a": 1\n  "b" x', 7],
  ['a sequence diagram', '---\ntitle: x\n---\nsequenceDiagram\n  %% c\n  A->>B: hi\n  A-x\n', 7],
  ['bad YAML in the front matter', '---\ntitle: [x\n---\nflowchart TD\n  A-->B', 3],
  ['an unclosed bracket', 'flowchart TD\n  A[x] --> B[\n  B-->C', 2],
  ['an unknown diagram type', '%% x\nflowchat TD\n  A-->B', 2],
];
