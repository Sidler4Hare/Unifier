"""Wait for the two PulseAudio listings to describe one usable playback route."""
import time


class PlaybackRouteError(RuntimeError):
    def __init__(self, message, observations):
        super().__init__(message)
        self.observations = observations


def wait_for_playback_route(tag, player, read_pulse, *, timeout=6, pause=time.sleep,
                            clock=time.monotonic):
    observations = []
    deadline = clock() + timeout
    while clock() < deadline:
        found = [s for s in read_pulse('sink-inputs')
                 if s['properties'].get('application.name') == tag]
        if found:
            candidate = found[0]
            sinks = read_pulse('sinks')
            matched = [s for s in sinks if s['index'] == candidate['sink']]
            observations.append({'sink_input_index': candidate['index'],
                                 'sink_reference': candidate['sink'],
                                 'available_sink_indices': [s['index'] for s in sinks],
                                 'corked': candidate['corked'], 'mute': candidate['mute']})
            if matched and not candidate['corked'] and not candidate['mute']:
                return candidate, matched[0], observations
        if player.poll() is not None:
            raise PlaybackRouteError('audio player exited before a usable route was observed', observations)
        pause(0.03)
    raise PlaybackRouteError('no bound, unmuted and uncorked playback route before timeout', observations)
