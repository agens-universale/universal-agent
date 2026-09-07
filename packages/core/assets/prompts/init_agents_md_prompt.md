You are setting up an AGENTS.md file that documents this directory for future agent
sessions. AGENTS.md is read by every future agent run here, so its job is to orient
them fast — not to restate what they can discover on their own.

First look at what this directory actually is. It may be a software project, or a
general working folder (documents, research, notes, data, operations). Document it for
what it is — do not force a code framing onto a folder that has no code.

Scan the directory to understand it (read the key files: README and other top-level
docs, build/config files, the main contents). Then WRITE the file `AGENTS.md` in the
directory root using the `write_to_file` tool. You MUST actually call `write_to_file`
to create the file — do not just print its content.

If it is a software project, organize AGENTS.md under:
- Project overview — what it is and does, in a few lines.
- Commands — the real build / test / run / lint commands, including how to run a single
  test. List only commands that actually exist here.
- Architecture — the big-picture structure that takes reading several files to grasp:
  key modules, entry points, how the main flow connects. Explain connections, not a file tree.
- Conventions — style, naming, and patterns specific to this codebase.
- Important paths — directories and files an agent must know about, and any it must not touch.

Otherwise (a general working folder), organize it under:
- Overview — what this folder is for and what lives in it.
- How work is done here — the common tasks and how they are typically carried out, and
  any tools, formats, or routines used.
- Structure — how the contents are organized and where things belong.
- Conventions — naming, formatting, and patterns specific to this folder.
- Important paths — items an agent must know about, and any it must not touch.

If a README or other top-level docs exist, fold in the parts that matter; do not copy
them verbatim.

Write tightly and usefully:
- Do not repeat yourself.
- Do not include generic advice or obvious instructions — only what is true and specific
  to this directory.
- Do not enumerate everything that is trivially discoverable by listing the folder;
  capture what cannot be learned at a glance.

{existing_block}

After the file is written, reply with a single short line confirming the path you wrote.
Do not paste the full file content in your reply.
