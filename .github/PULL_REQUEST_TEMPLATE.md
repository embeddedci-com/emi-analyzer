## What and why

<!-- What changes, and the problem it solves. Link the issue if there is one. -->

## How it was checked

<!-- Tests added or run, and anything checked by hand (which board, which run kind). -->

- [ ] `make test` (Go and worker), and `cd webapp && npm test && npm run typecheck` if the webapp changed
- [ ] `make plugin-test` if `kicad-plugin/` changed
- [ ] Shared fixtures regenerated (`make fixtures`, `make driver-fixtures`, `make component-fixtures`) if the cost model, driver spectrum or component library changed
- [ ] No board files or data that are not yours to share
