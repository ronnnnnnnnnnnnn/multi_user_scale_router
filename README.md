# Multi-User Scale Router for Home Assistant

This custom integration automatically routes weight measurements from any connected smart scale to the correct household member. It compares each new reading against user weight history and either assigns it automatically if there's just one likely match or holds it as a pending measurement otherwise — sending actionable mobile push notifications when the match is ambiguous.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## Features

- Works with any Home Assistant weight sensor (no hardware access required — this integration reads from an existing sensor entity)
- Intelligent multi-user support:
    - Automatically detects which person is using the scale based on their weight history
    - Uses a configurable tolerance system to handle natural weight fluctuations
    - Supports linking users to Home Assistant Person entities to exclude users who are `not_home`
- Actionable mobile push notifications for ambiguous measurements with one-tap assignment buttons
- Services to manually assign, reassign, and remove measurements
- Configurable history retention period and maximum history size
- Measurement history is persisted across Home Assistant restarts

## Notes

- A scale integration that exposes a weight sensor entity in Home Assistant must already be set up before adding this integration.
- This integration does not communicate with any hardware directly. It reads from an existing sensor and assigns each reading to the appropriate user.

## Installation

### HACS (Recommended)

> **Note:** This integration is not yet in the HACS default store. You must add it as a custom repository first (see below), or use the button which does this automatically.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ronnnnnnnnnnnnn&repository=multi_user_scale_router&category=integration)

1. Ensure that [HACS](https://hacs.xyz/) is installed in your Home Assistant instance.
2. Click the button above, **or** add this repository manually:
   - In HACS, go to **Integrations** → click the three-dot menu (⋮) → **Custom repositories**
   - Enter `https://github.com/ronnnnnnnnnnnnn/multi_user_scale_router` and select **Integration** as the category
3. Search for "Multi-User Scale Router" in HACS and click **Download**.
4. Restart Home Assistant.

### Manual Installation

1. Copy the `multi_user_scale_router` folder to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.

## Configuration

### Initial Setup

1. In Home Assistant, go to **Settings → Devices & Services**.
2. Click **Add Integration** and search for "Multi-User Scale Router".
3. Select the weight sensor entity from your scale integration as the source.
4. Add at least one user profile.
5. Optionally adjust advanced settings (history size, retention period, weight tolerance).

### User Profile Configuration Options

When adding or editing user profiles (**Settings → Devices & Services → Multi-User Scale Router → Configure**), you can configure the following options:

- **User Name:** Display name for the user profile.

- **Person Entity (optional):** Link this user profile to a Home Assistant person entity. When linked, the integration uses the person's location state to improve automatic assignment:
    - If the person is marked as `not_home`, they are excluded from automatic assignment for new measurements
    - This helps avoid incorrectly assigning measurements when household members are away

- **Notification Services (optional):** Enter one or more Home Assistant notify service names (e.g. `notify.mobile_app_my_phone`) to receive actionable notifications for ambiguous measurements:
    - When enabled, you'll receive a mobile notification with tap-to-assign buttons directly on your phone
    - Each candidate user gets a personalised notification

## Multi-User Support

This integration is designed for households with multiple people sharing a scale. You can create a unique profile for each person.

### Person Detection

When a new measurement is received, the integration attempts to automatically assign it to the correct person based on two factors:

1. **Weight History:** The measurement is compared against each user's weight history using a configurable tolerance window.
2. **Location:** If a user profile is linked to a Home Assistant `person` entity, the integration checks if that person is `home`. Users who are `not_home` are excluded from automatic assignment.

If a single user is a clear match, the measurement is assigned automatically.

### Ambiguous Measurements

If the measurement is ambiguous (e.g. two users have similar weights, or a new user has no history yet), the integration will notify you:

- **Mobile Notifications (if configured):** Each candidate user receives a personalised notification on their mobile device with actionable buttons:
    - "Assign to Me" / "Assign to {Name}" — Assigns the measurement immediately
    - "Not Me" / "Not {Name}" — Removes you as a candidate; if you are the last candidate the measurement is discarded

- **Persistent Notifications:** A notification appears in the Home Assistant notifications panel with instructions to manually assign the measurement using the `assign_measurement` service.

### Managing Users

Navigate to **Settings → Devices & Services → Multi-User Scale Router** and click **Configure** to:
- **Add a new user:** Create a new profile with an optional person entity link and notification settings.
- **Edit a user:** Update a user's name, linked person entity, or notification services.
- **Remove a user:** Delete a user's profile and all associated sensor entities.

## Services

The integration provides services to manage measurements. You can use these in scripts or automations, or call them directly from **Developer Tools → Actions**.

### `multi_user_scale_router.assign_measurement`

Assign a pending (ambiguous) measurement to a specific user. The `measurement_id` and candidate `user_id`s are listed in the Pending Measurements entity attributes and in the persistent notification.

**Example:**
```yaml
action: multi_user_scale_router.assign_measurement
data:
  device_id: <your_scale_router_device_id>
  measurement_id: "abc123def456"
  user_id: "jane"
```

### `multi_user_scale_router.reassign_measurement`

Move a measurement from one user to another. Useful if a measurement was automatically but incorrectly assigned.

**Example:**
```yaml
action: multi_user_scale_router.reassign_measurement
data:
  device_id: <your_scale_router_device_id>
  from_user_id: "john"
  to_user_id: "jane"
  measurement_id: "abc123def456"  # optional; omit to reassign the most recent
```

### `multi_user_scale_router.remove_measurement`

Remove a measurement from a user's history. The user's sensor reverts to their previous value.

**Example:**
```yaml
action: multi_user_scale_router.remove_measurement
data:
  device_id: <your_scale_router_device_id>
  user_id: "john"
  measurement_id: "abc123def456"  # optional; omit to remove the most recent
```

## Diagnostic Sensors

The integration creates two diagnostic sensors to provide visibility into its state:

- **User Directory:** Shows the number of configured user profiles and lists their details (including `user_id`) in the attributes.
- **Pending Measurements:** Shows the number of ambiguous measurements awaiting manual assignment and lists their details in the attributes.

## Troubleshooting

- If measurements are not being picked up, confirm that the source weight sensor is updating correctly in Home Assistant and that its state is a numeric value with a weight unit.
- If automatic assignment seems incorrect, check the User Directory diagnostic sensor — the `history_count` for each user shows how many measurements this integration has to compare against. Assignment accuracy improves as more measurements are collected.
- If you encounter any issues, check the Home Assistant logs (filtering for `multi_user_scale_router`) for more information.

## Support the Project

If you find this project helpful, consider buying me a coffee! Your support helps maintain and improve this integration.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
