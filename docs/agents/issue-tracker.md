# Issue tracker: Linear

Issues and specs for this repo live in Linear, team **Studiohays** (issue key `STU`, e.g. `STU-5`). Use the `linear-server` MCP tools (`mcp__linear-server__*`) for every operation; don't use `gh issue`.

## Conventions

- **Create an issue**: `save_issue` with `team: "Studiohays"`, `title` and a Markdown `description` (real newlines, not `\n`). New issues start in **Backlog** unless a skill says otherwise.
- **Read an issue**: `get_issue` with the identifier (e.g. `STU-5`) and `includeRelations: true`, then `list_comments` with `issueId`.
- **List issues**: `list_issues` with `team: "Studiohays"`, filtered by `label`, `state`, `project` or `query`.
- **Comment**: `save_comment` with `issueId` and `body`.
- **Apply / remove labels**: `save_issue` with `id` and `addLabels` / `removeLabels`. Never use `labels`, which replaces the whole set.
- **Change status**: `save_issue` with `id` and `state` (Backlog, Todo, In Progress, Done, Canceled, Duplicate).
- **Close**: `save_comment` with the reason, then `save_issue` with `state: "Done"`, or `"Canceled"` if it won't be done.
- **Projects**: if the work belongs to one `lib/` project (e.g. studio-hays), set `project` on the issue when a matching Linear project exists. Don't create projects unprompted.

Refer to issues by their Linear identifier (`STU-<n>`). Put it in commit messages and PR bodies when work closes an issue.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if external GitHub PRs should be treated as feature requests; `/triage` reads this flag.)_

## When a skill says "publish to the issue tracker"

Create a Linear issue in team Studiohays with `save_issue`.

## When a skill says "fetch the relevant ticket"

Call `get_issue` with `includeRelations: true`, then `list_comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is one parent issue; each ticket is a **sub-issue** of it.

- **Map**: an issue labelled `wayfinder:map` whose description holds the Notes, Decisions-so-far and Fog sections.
- **Child ticket**: `save_issue` with `parentId: <map id>` and a `wayfinder:<type>` label (`research` / `prototype` / `grilling` / `task`).
- **Blocking**: Linear's native relations. `save_issue` with `blockedBy: [<ids>]` on the child. A ticket is unblocked when every blocker is Done or Canceled.
- **Frontier query**: `list_issues` with `parentId: <map id>` and an open state. Drop any issue with an open blocker (`get_issue` with `includeRelations: true`) or with an assignee. The first remaining issue in map order wins.
- **Claim**: `save_issue` with `id`, `assignee: "me"` and `state: "In Progress"`. This is the session's first write.
- **Resolve**: `save_comment` with the answer, then `save_issue` with `state: "Done"`, then append a context pointer (gist + link) to the map's Decisions-so-far with a `patch` append.
