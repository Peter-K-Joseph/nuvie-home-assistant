# Nuvie Planner for Home Assistant

The house you planned in [Nuvie Planner](https://planner.nuvie.in), in Home Assistant:

- **Floors and rooms.** The plan's storeys and rooms become Home Assistant floors and areas. Ones with the same name are reused; nothing is renamed or deleted.
- **Devices in their rooms.** Lights and devices you linked to an entity in Nuvie go into their room, if they don't have one yet.
- **A 3D card** (`custom:nuvie-home-card`): the house with every linked device on it, live. Lights that are on glow and light their room, and tapping a light, switch or fan toggles it.
- **Security at a glance.** A triggered alarm or smoke turns the house red and names the room, motion lights up its room, a doorbell ring shows a banner, and a camera button shows what each camera covers.
- **Always up to date.** Connected to your Nuvie account, Home Assistant checks for a newer plan every 10 minutes and syncs it on its own.

## Install

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Peter-K-Joseph&repository=nuvie-home-assistant&category=integration)

1. Click the button above (or in HACS: ⋮ → **Custom repositories**, add this repository as an **Integration**). Download **Nuvie Planner**, then restart Home Assistant.
2. [![Add the integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nuvie) or go to **Settings → Devices & services → Add integration → Nuvie Planner**.
3. Pick **Connect to my Nuvie account**. Home Assistant shows a link and a code: open the link, sign in to Nuvie, pick the project and click **Connect**. Home Assistant finishes on its own.
4. Edit a dashboard, add a card and pick **Nuvie home (3D)**.

Your Nuvie password never goes to Home Assistant: Nuvie gives it a token for that one project. You can disconnect it any time in Nuvie under **Settings → Home Assistant**, or by removing the integration here.

**Updates.** After you save the project in Nuvie, Home Assistant picks up the change within 10 minutes. To get it straight away, run the **Nuvie Planner: Sync from the plan** action (`nuvie.sync`).

**No account link?** You can still use **Export → Smart home → Home Assistant** in Nuvie, unzip it into the config folder, and choose **Use an export I unzipped** in step 3.

## The card

```yaml
type: custom:nuvie-home-card
# title: Home           # default: the project name ("" hides it)
# floor: ground_floor   # the storey to open on (floor_id or name)
# height: 480           # px
# show_unlinked: false  # also pin items that aren't linked (grey)
# show_summary: true    # the "what's on" list
# quality: auto         # auto | saver | balanced | high | ultra
# time: live            # live (the real sun) | studio (fixed daylight) | "18:30"
# weather: live         # live | clear | partly | overcast | rain | fog
# weather_entity: weather.home
# show_settings: true   # the sliders button (quality, time, weather per device)
# show_coverage: false  # draw what each camera sees
```

Admins get a pencil button on the card to link any device to an entity from a list. Those links are kept in Home Assistant and win over the plan's.

Needs Home Assistant 2024.12 or later.

This repository is published from the Nuvie Planner source; please don't send pull requests here. Report problems in [Issues](https://github.com/Peter-K-Joseph/nuvie-home-assistant/issues).
