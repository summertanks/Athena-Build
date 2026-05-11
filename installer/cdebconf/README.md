# installer/cdebconf/

cdebconf engine configuration shipped into the installer chroot at
`/etc/cdebconf.conf` (when a file `cdebconf.conf` is present here).

## What does cdebconf.conf do?

It controls the question-and-answer engine that drives the entire d-i
flow: which frontend renders prompts, where the questions/answers/
passwords databases live on disk, and which plugins cdebconf loads.

Format is bind-style (curly braces, semicolon-terminated):

```
global {
    module_path { frontend "/usr/lib/cdebconf/frontend"; };
    default     { frontend "newt"; template "templatedb"; config "configdb"; };
};
```

## v1 status

**No `cdebconf.conf` shipped.**  cdebconf-udeb's baked-in default
(`driver newt`) wins, which matches what every Debian installer does
out of the box.  If we hit a frontend-selection issue, drop a file here
and the engine will copy it to `/etc/cdebconf.conf` in the chroot.

Common overrides if needed later:
- Force text frontend: change `default { frontend "text"; ... }`
- Custom plugin path: change `module_path`
- Move questions DB off `/var/lib/cdebconf/`: edit `template`/`config`/`passwords`

The runtime frontend can also be overridden via `DEBIAN_FRONTEND=text`
env or kernel cmdline — no config file needed for that single flip.
