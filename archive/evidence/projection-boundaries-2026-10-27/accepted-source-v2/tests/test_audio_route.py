"""Regression for the unbound sink-input observed during the live media probe."""
import unittest

from tools.audio_route import PlaybackRouteError, wait_for_playback_route


def stream(sink, **overrides):
    return {'index': 10, 'sink': sink, 'corked': False, 'mute': False,
            'properties': {'application.name': 'own-tone'}, **overrides}


class Player:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class RouteReadiness(unittest.TestCase):
    def test_new_stream_can_exist_before_its_sink_is_listed(self):
        rounds = iter([(stream(4294967295), [{'index': 7}]),
                       (stream(7), [{'index': 7}])])
        current = None
        def read(kind):
            nonlocal current
            if kind == 'sink-inputs':
                current = next(rounds)
                return [current[0]]
            return current[1]
        selected, sink, observations = wait_for_playback_route(
            'own-tone', Player(), read, pause=lambda _: None)
        self.assertEqual((selected['sink'], sink['index']), (7, 7))
        self.assertEqual([o['sink_reference'] for o in observations], [4294967295, 7])

    def test_corked_stream_is_not_accepted_as_playing(self):
        states = iter([True, False])
        def read(kind):
            return [stream(7, corked=next(states))] if kind == 'sink-inputs' else [{'index': 7}]
        _, _, observations = wait_for_playback_route('own-tone', Player(), read, pause=lambda _: None)
        self.assertEqual([o['corked'] for o in observations], [True, False])

    def test_another_app_stream_is_not_used_when_our_player_exits(self):
        def read(kind):
            return [stream(7, properties={'application.name': 'unrelated-app'})]
        with self.assertRaises(PlaybackRouteError) as result:
            wait_for_playback_route('own-tone', Player(0), read)
        self.assertEqual(result.exception.observations, [])

    def test_timeout_does_not_turn_missing_route_into_zero_metrics(self):
        with self.assertRaises(PlaybackRouteError) as result:
            wait_for_playback_route('own-tone', Player(), lambda _: [], timeout=0)
        self.assertIn('timeout', str(result.exception))


if __name__ == '__main__':
    unittest.main()
