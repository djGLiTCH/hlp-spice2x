# Profiles

A profile maps one game's light names onto controls on your board. Light names
come from the game, so a profile is per-game, but it is not per-board: naming
controls rather than LED indexes means the firmware resolves them to whatever
LEDs that board actually has.

`example.json` is illustrative only. Its names are made up, and no real game
reports exactly those.

## Capturing one

Start the game under spice2x with the API enabled, then:

```
hlp-spice2x --port 1337 --list-lights
```

That prints every light and its current value. Pressing buttons and watching
the values change is the quickest way to work out which name is which.

Then generate a skeleton with every light listed:

```
hlp-spice2x --port 1337 --write-profile mygame.json
```

Fill in the `button` field for the lights you want and leave the rest empty.
Check the result without touching a board:

```
hlp-spice2x --port 1337 --profile mygame.json --dry-run
```

## Contributing

Pull requests adding a profile are welcome. Please name the file after the
game, and note in the PR which game and version you captured it from, since
light names can change between releases.
