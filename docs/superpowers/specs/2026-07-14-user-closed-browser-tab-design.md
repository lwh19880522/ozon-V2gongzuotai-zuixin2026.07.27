# User-Closed Browser Tab Design

## Goal

When a user closes the browser tab controlled by an active Ozon V2 collection task, stop that browser task and keep background polling quiet until the user explicitly resumes the batch from the workbench.

## Behavior

- The extension records a task-scoped close marker when an assigned tab is removed.
- The extension calls the existing `runner/stop` endpoint so the workbench and browser agree that collection is stopped.
- Background and workbench polling must not create, navigate, or reassign a tab while the current dispatch token matches the close marker.
- Clicking `Continue Autopilot` clears the server cancellation and produces a new dispatch token. The new token clears the close marker and permits one tab to open again.
- Completed tasks are not cancelled if their former tab is closed after the server has already advanced.

## Boundaries

- No changes to collection evidence, seed selection, or batch artifacts.
- No permanent global disable switch; suppression is scoped to one task dispatch token.
- No repeated prompts or automatic retries after a user closes the assigned tab.

## Verification

- Browser background test proves closing the assigned tab posts a stop request.
- The same dispatch token cannot create a replacement tab.
- A resumed task with a new dispatch token may create a replacement tab exactly once.

