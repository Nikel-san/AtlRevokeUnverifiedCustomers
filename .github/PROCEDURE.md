# General Procedure for Copilot


**Note:** This is a local copy with enhanced credit-efficiency rules. See the original Confluence page for the source version.

## Core Principles
1. Never assume anything, always check; in doubt - ask.
2. **Never deviate from the General Procedure.**
3. For Jira reads, start with minimal fields (`summary`, `description`, `comment`, `issuelinks`, `status`, `labels`) and fetch additional fields only when strictly required. Note the repository path from the ## Repository section. If the repo path is missing, ask before proceeding.

## Git & Repository Workflow
4. Before making any changes, run `git pull` to ensure the local repository is up to date with the remote. Use the repo path from the ticket.
5. Implement everything required end to end. Never hardcode credentials, site URLs, or domain names — always use ATLASSIAN_SITE, JIRA_EMAIL, JIRA_PAT environment variables.
6. Validate all required environment variables at script startup. If any are missing, print a clear error message and exit with a non-zero code. Never pass `None` or empty strings to API calls.
7. Verify the script runs without errors (syntax check or dry-run) before committing.
8. Commit and push changes to the GitHub remote and ensure the local copy of the repository is up to date. Use the repo path from the ticket.

## Jira Workflow
9. Comment on the ticket with exactly what was done — list each bug fixed and each feature added separately with a brief description. You must use `POST /rest/api/3/issue/{key}/comment` separately, not pass `comment` inside an issue update body. The comment body MUST be in Atlassian Document Format (ADF), not a plain string.
   - Example of a valid request body:
   ```json
   {
     "body": {
       "type": "doc",
       "version": 1,
       "content": [
         {
           "type": "paragraph",
           "content": [
             {"type": "text", "text": "Your comment text here"}
           ]
         }
       ]
     }
   }
   ```
   - Do NOT pass a plain string as the body value — Jira Cloud API v3 will reject it with "Comment body is not valid!" error.

10. Transition the ticket to **In Review**, then add the `ai-review` label to the ticket.
11. If fixing a reopened or previously reviewed ticket, make the code changes, verify them, commit and push, add a new completion comment, transition the ticket to **In Review**, and add or re-add the `ai-review` label.

## Code Quality & Safety
12. Never create test environments, test scripts, test files (test_*.py), mock data files, or leave debug blocks in production code.
13. Always use the Atlassian Rovo MCP server to read from or write to Jira. Do not use any other method. If the MCP call fails, print the result and likely reason, and stop - do not fall back to alternative methods silently.
14. At the end, write a short summary with the result for each item.

## Credit-Efficient Execution Rules

### Core Principles
15. Minimize tool usage while preserving correctness. Use the smallest number of calls needed to complete the task.
16. Do not duplicate Jira fetches or parse large response artifacts if required information is already available.
17. Use a single dependency installation path per task (either environment package tool flow or terminal package install), not both.
22. **Hard start gate for ticket work:** first call must retrieve the Jira issue details using Jira issue tools with minimal fields; do not call unrelated Atlassian tools before this succeeds.
24. Do not use external repository/documentation lookups when the ticket already provides sufficient implementation requirements.

### Consolidation & Batching (MANDATORY)
26a. **MANDATORY: Use multi_replace_string_in_file for ALL independent file edits** on the same or different files. Never make sequential replace_string_in_file calls when edits can be parallelized. This is a hard requirement.
26b. Consolidate independent tool_search calls into single queries using OR patterns (e.g., "jira issue get AND edit AND transition") to load multiple tools at once. Never make sequential tool_search calls for related tools.
26c. If a tool was recently loaded via tool_search in the same task, reuse it without additional tool_search calls. tool_search results remain available for the session.

### Execution & Validation
18. **Single focused validation pass:** Run one comprehensive diagnostics pass after ALL code changes are complete, not incrementally. Examples: syntax check + error handling test in one run, not separate runs.
19. Avoid repeated repository checks; perform git status checks only at key checkpoints (before commit and after push).
20. **REQUIRED: Execute all work in one cohesive pass** — pull → read required files → batch all edits → single validation → commit → push → Jira updates. Never iterate incrementally within a task.
21. Keep verification scoped to acceptance criteria; avoid extra exploratory commands unless they are needed to unblock completion.

### File & Data Access Discipline
25. Never read files that will be completely replaced or regenerated. If you plan to rewrite entire file content from scratch, skip the read step entirely.
27. Do not perform exploratory directory listings or file reads if required information is already present in the Jira ticket description, repository documentation, or README. Only read when resolving ambiguity.
28. When reading Jira responses, parse once and reuse the data. Do not make additional fetches to extract information already in previous responses.

### Error Recovery
23. If a tool call is clearly wrong for the task, stop exploratory retries immediately and switch to the known-correct tool path.

### Standard Ticket Sequence
29. Use this fixed minimum sequence for standard tickets: 
    1. Jira read (minimal fields)
    2. git pull
    3. Read target files (only if not fully replacing)
    4. Batch all file edits
    5. One focused validation pass
    6. git commit & push
    7. Jira comment (ADF format)
    8. Jira transition
    9. Jira label update

### Communication
30. Keep progress updates concise and low-token by default; expand only when blocked or when a decision requires user input.

## Anti-Patterns to Avoid

These patterns waste tokens and violate credit-efficiency rules:

- **Sequential tool_search calls** → Use OR patterns to load multiple related tools at once
- **Sequential replace_string_in_file calls** → Use multi_replace_string_in_file to batch edits
- **Multiple validation passes** → Single focused pass after all changes complete
- **Exploratory reads of files being replaced** → Skip the read if rewriting entire file
- **Exploratory directory/file reads** → Only read if info is not in ticket or existing docs
- **Multiple git status checks** → Check only at key checkpoints (before commit, after push)
- **Incremental code changes and testing** → Complete all edits first, then validate once
- **Reading large Jira responses multiple times** → Parse once, reuse extracted data
