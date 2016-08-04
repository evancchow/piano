"""
Sample mixer and sequencer meant to create rhythms. Inspired by the Roland TR-909.
Uses PyAudio (https://pypi.python.org/pypi/PyAudio) for playing sound. On windows
it can fall back to using the winsound module if pysound isn't available.

Sample mix rate is configured at 44.1 khz. You may want to change this if most of
the samples you're using are of a different sample rate (such as 48Khz), to avoid
the slight loss of quality due to resampling.

Written by Irmen de Jong (irmen@razorvine.net) - License: MIT open-source.
"""

import sys
import os
import wave
import audioop
import array
import threading
import queue
import math
import itertools
try:
    import pyaudio
except ImportError:
    pyaudio = None
    import winsound
try:
    import numpy
except ImportError:
    numpy = None


__all__ = ["Sample", "Output", "LevelMeter"]


samplewidths_to_arraycode = {
    1: 'b',
    2: 'h',
    4: 'l'    # or 'i' on 64 bit systems
}

# the actual array type code for the given sample width varies
if array.array('i').itemsize == 4:
    samplewidths_to_arraycode[4] = 'i'


class Sample:
    """
    Audio sample data. Supports integer sample formats of 2, 3 and 4 bytes per sample (no floating-point).
    Python 3.4+ is required to support 3-bytes/24-bits sample sizes.
    Most operations modify the sample data in place (if it's not locked) and return the sample object,
    so you can easily chain several operations.
    """
    norm_samplerate = 44100
    norm_nchannels = 2
    norm_samplewidth = 2

    def __init__(self, wave_file=None):
        """Creates a new empty sample, or loads it from a wav file."""
        self.__locked = False
        if wave_file:
            self.load_wav(wave_file)
            self.__filename = wave_file
            assert 1 <= self.__nchannels <= 2
            assert 2 <= self.__samplewidth <= 4
            assert self.__samplerate > 1
        else:
            self.__samplerate = self.norm_samplerate
            self.__nchannels = self.norm_nchannels
            self.__samplewidth = self.norm_samplewidth
            self.__frames = b""
            self.__filename = None

    def __repr__(self):
        locked = " (locked)" if self.__locked else ""
        return "<Sample at 0x{0:x}, {1:g} seconds, {2:d} channels, {3:d} bits, rate {4:d}{5:s}>"\
            .format(id(self), self.duration, self.__nchannels, 8*self.__samplewidth, self.__samplerate, locked)

    def __eq__(self, other):
        if not isinstance(other, Sample):
            return False
        return self.__samplewidth == other.__samplewidth and \
            self.__samplerate == other.__samplerate and \
            self.__nchannels == other.__nchannels and \
            self.__frames == other.__frames

    @classmethod
    def from_raw_frames(cls, frames, samplewidth, samplerate, numchannels):
        """Creates a new sample directly from the raw sample data."""
        assert 1 <= numchannels <= 2
        assert 2 <= samplewidth <= 4
        assert samplerate > 1
        s = cls()
        s.__frames = frames
        s.__samplerate = int(samplerate)
        s.__samplewidth = int(samplewidth)
        s.__nchannels = int(numchannels)
        return s

    @classmethod
    def from_array(cls, array_or_list, samplerate, numchannels):
        assert 1 <= numchannels <= 2
        assert samplerate > 1
        if isinstance(array_or_list, list):
            try:
                array_or_list = Sample.get_array(2, array_or_list)
            except OverflowError:
                array_or_list = Sample.get_array(4, array_or_list)
        elif numpy:
            if isinstance(array_or_list, numpy.ndarray) and any(array_or_list):
                if not isinstance(array_or_list[0], (int, numpy.integer)):
                    raise TypeError("the sample values must be integer")
        else:
            if any(array_or_list):
                if type(array_or_list[0]) is not int:
                    raise TypeError("the sample values must be integer")
        samplewidth = array_or_list.itemsize
        assert 2 <= samplewidth <= 4
        frames = array_or_list.tobytes()
        if sys.byteorder == "big":
            frames = audioop.byteswap(frames, samplewidth)
        return Sample.from_raw_frames(frames, samplewidth, samplerate, numchannels)

    @property
    def samplewidth(self):
        return self.__samplewidth

    @property
    def samplerate(self):
        """You can also set this to a new value, but that will directly affect the pitch and the duration of the sample."""
        return self.__samplerate

    @samplerate.setter
    def samplerate(self, rate):
        assert rate > 0
        self.__samplerate = int(rate)

    @property
    def nchannels(self): return self.__nchannels

    @property
    def filename(self): return self.__filename

    @property
    def duration(self):
        return len(self.__frames) / self.__samplerate / self.__samplewidth / self.__nchannels

    @property
    def maximum(self):
        return audioop.max(self.__frames, self.samplewidth)

    @property
    def rms(self):
        return audioop.rms(self.__frames, self.samplewidth)

    @property
    def level_db_peak(self):
        return self.__db_level(False)

    @property
    def level_db_rms(self):
        return self.__db_level(True)

    def __db_level(self, rms_mode=False):
        """
        Returns the average audio volume level measured in dB (range -60 db to 0 db)
        If the sample is stereo, you get back a tuple: (left_level, right_level)
        If the sample is mono, you still get a tuple but both values will be the same.
        This method is probably only useful if processed on very short sample fragments in sequence,
        so the db levels could be used to show a level meter for the duration of the sample.
        """
        maxvalue = 2**(8*self.__samplewidth-1)
        if self.nchannels == 1:
            if rms_mode:
                peak_left = peak_right = (audioop.rms(self.__frames, self.__samplewidth)+1)/maxvalue
            else:
                peak_left = peak_right = (audioop.max(self.__frames, self.__samplewidth)+1)/maxvalue
        else:
            left_frames = audioop.tomono(self.__frames, self.__samplewidth, 1, 0)
            right_frames = audioop.tomono(self.__frames, self.__samplewidth, 0, 1)
            if rms_mode:
                peak_left = (audioop.rms(left_frames, self.__samplewidth)+1)/maxvalue
                peak_right = (audioop.rms(right_frames, self.__samplewidth)+1)/maxvalue
            else:
                peak_left = (audioop.max(left_frames, self.__samplewidth)+1)/maxvalue
                peak_right = (audioop.max(right_frames, self.__samplewidth)+1)/maxvalue
        # cut off at the bottom at -60 instead of all the way down to -infinity
        return max(20.0*math.log(peak_left, 10), -60.0), max(20.0*math.log(peak_right, 10), -60.0)

    def __len__(self):
        """returns the number of sample frames"""
        return len(self.__frames) // self.__samplewidth // self.__nchannels

    def get_frame_array(self):
        """Returns the sample values as array. Warning: this can copy large amounts of data."""
        return Sample.get_array(self.samplewidth, self.__frames)

    @staticmethod
    def get_array(samplewidth, initializer=None):
        """Returns an array with the correct type code, optionally initialized with values."""
        return array.array(samplewidths_to_arraycode[samplewidth], initializer or [])

    def copy(self):
        """Returns a copy of the sample (unlocked)."""
        cpy = Sample()
        cpy.copy_from(self)
        return cpy

    def copy_from(self, other):
        """Overwrite the current sample with a copy of the other."""
        assert not self.__locked
        self.__frames = other.__frames
        self.__samplewidth = other.__samplewidth
        self.__samplerate = other.__samplerate
        self.__nchannels = other.__nchannels
        self.__filename = other.__filename
        return self

    def lock(self):
        """Lock the sample against modifications."""
        self.__locked = True
        return self

    def frame_idx(self, seconds):
        """Calculate the raw frame index for the sample at the given timestamp."""
        return self.nchannels*self.samplewidth*int(self.samplerate*seconds)

    def load_wav(self, file_or_stream):
        """Loads sample data from the wav file. You can use a filename or a stream object."""
        assert not self.__locked
        with wave.open(file_or_stream) as w:
            if not 2 <= w.getsampwidth() <= 4:
                raise IOError("only supports sample sizes of 2, 3 or 4 bytes")
            if not 1 <= w.getnchannels() <= 2:
                raise IOError("only supports mono or stereo channels")
            self.__frames = w.readframes(w.getnframes())
            self.__nchannels = w.getnchannels()
            self.__samplerate = w.getframerate()
            self.__samplewidth = w.getsampwidth()
            return self

    def write_wav(self, file_or_stream):
        """Write a wav file with the current sample data. You can use a filename or a stream object."""
        with wave.open(file_or_stream, "wb") as out:
            out.setparams((self.nchannels, self.samplewidth, self.samplerate, 0, "NONE", "not compressed"))
            out.writeframes(self.__frames)

    @classmethod
    def wave_write_begin(cls, filename, first_sample):
        """
        Part of the sample stream output api: begin writing a sample to an output file.
        Returns the open file for future writing.
        """
        out = wave.open(filename, "wb")
        out.setparams((first_sample.nchannels, first_sample.samplewidth, first_sample.samplerate, 0, "NONE", "not compressed"))
        out.writeframesraw(first_sample.__frames)
        return out

    @classmethod
    def wave_write_append(cls, out, sample):
        """Part of the sample stream output api: write more sample data to an open output stream."""
        out.writeframesraw(sample.__frames)

    @classmethod
    def wave_write_end(cls, out):
        """Part of the sample stream output api: finalize and close the open output stream."""
        out.writeframes(b"")  # make sure the updated header gets written
        out.close()

    def write_frames(self, stream):
        """Write the raw sample data to the output stream."""
        stream.write(self.__frames)

    def normalize(self):
        """
        Normalize the sample, meaning: convert it to the default samplerate, sample width and number of channels.
        When mixing samples, they should all have the same properties, and this method is ideal to make sure of that.
        """
        assert not self.__locked
        self.resample(self.norm_samplerate)
        if self.samplewidth != self.norm_samplewidth:
            # Convert to 16 bit sample size.
            self.__frames = audioop.lin2lin(self.__frames, self.samplewidth, self.norm_samplewidth)
            self.__samplewidth = self.norm_samplewidth
        if self.nchannels == 1:
            # convert to stereo
            self.__frames = audioop.tostereo(self.__frames, self.samplewidth, 1, 1)
            self.__nchannels = 2
        return self

    def resample(self, samplerate):
        """
        Resamples to a different sample rate, without changing the pitch and duration of the sound.
        The algorithm used is simple, and it will cause a loss of sound quality.
        """
        assert not self.__locked
        if samplerate == self.__samplerate:
            return self
        self.__frames = audioop.ratecv(self.__frames, self.samplewidth, self.nchannels, self.samplerate, samplerate, None)[0]
        self.__samplerate = samplerate
        return self

    def speed(self, speed):
        """
        Changes the playback speed of the sample, without changing the sample rate.
        This will change the pitch and duration of the sound accordingly.
        The algorithm used is simple, and it will cause a loss of sound quality.
        """
        assert not self.__locked
        assert speed > 0
        if speed == 1.0:
            return self
        rate = self.samplerate
        self.__frames = audioop.ratecv(self.__frames, self.samplewidth, self.nchannels, int(self.samplerate*speed), rate, None)[0]
        self.__samplerate = rate
        return self

    def make_32bit(self, scale_amplitude=True):
        """
        Convert to 32 bit integer sample width, usually also scaling the amplitude to fit in the new 32 bits range.
        Not scaling the amplitude means that the sample values will remain in their original range (usually 16 bit).
        This is ideal to create sample value headroom to mix multiple samples together without clipping or overflow issues.
        Usually after mixing you will convert back to 16 bits using maximized amplitude to have no quality loss.
        """
        assert not self.__locked
        self.__frames = self.get_32bit_frames(scale_amplitude)
        self.__samplewidth = 4
        return self

    def get_32bit_frames(self, scale_amplitude=True):
        """Returns the raw sample frames scaled to 32 bits. See make_32bit method for more info."""
        if self.samplewidth == 4:
            return self.__frames
        frames = audioop.lin2lin(self.__frames, self.samplewidth, 4)
        if not scale_amplitude:
            # we need to scale back the sample amplitude to fit back into 24/16/8 bit range
            factor = 1.0/2**(8*abs(self.samplewidth-4))
            frames = audioop.mul(frames, 4, factor)
        return frames

    def make_16bit(self, maximize_amplitude=True):
        """
        Convert to 16 bit sample width, usually by using a maximized amplification factor to
        scale into the full 16 bit range without clipping or overflow.
        This is used for example to downscale a 32 bits mixed sample back into 16 bit width.
        """
        assert not self.__locked
        assert self.samplewidth >= 2
        if maximize_amplitude:
            self.amplify_max()
        if self.samplewidth > 2:
            self.__frames = audioop.lin2lin(self.__frames, self.samplewidth, 2)
            self.__samplewidth = 2
        return self

    def amplify_max(self):
        """Amplify the sample to maximum volume without clipping or overflow happening."""
        assert not self.__locked
        max_amp = audioop.max(self.__frames, self.samplewidth)
        max_target = 2 ** (8 * self.samplewidth - 1) - 2
        if max_amp > 0:
            factor = max_target/max_amp
            self.__frames = audioop.mul(self.__frames, self.samplewidth, factor)
        return self

    def amplify(self, factor):
        """Amplifies (multiplies) the sample by the given factor. May cause clipping/overflow if factor is too large."""
        assert not self.__locked
        self.__frames = audioop.mul(self.__frames, self.samplewidth, factor)
        return self

    def at_volume(self, volume):
        """
        Returns a copy of the sample at the given volume level 0-1, leaves original untouched.
        This is a special method (next to amplify) because often the same sample will be used
        at different volume levels, and it is cumbersome to drag copies around for every volume desired.
        This also enables you to use this on locked samples.
        """
        cpy = self.copy()
        cpy.amplify(volume)
        return cpy

    def clip(self, start_seconds, end_seconds):
        """Keep only a given clip from the sample."""
        assert not self.__locked
        assert end_seconds > start_seconds
        start = self.frame_idx(start_seconds)
        end = self.frame_idx(end_seconds)
        self.__frames = self.__frames[start:end]
        return self

    def split(self, seconds):
        """Splits the sample in two parts, keep the first and return the chopped off bit at the end."""
        assert not self.__locked
        end = self.frame_idx(seconds)
        if end != len(self.__frames):
            chopped = self.copy()
            chopped.__frames = self.__frames[end:]
            self.__frames = self.__frames[:end]
            return chopped
        return Sample.from_raw_frames(b"", self.__samplewidth, self.__samplerate, self.__nchannels)

    def add_silence(self, seconds, at_start=False):
        """Add silence at the end (or at the start)"""
        assert not self.__locked
        required_extra = self.frame_idx(seconds)
        if at_start:
            self.__frames = b"\0"*required_extra + self.__frames
        else:
            self.__frames += b"\0"*required_extra
        return self

    def join(self, other):
        """Add another sample at the end of the current one. The other sample must have the same properties."""
        assert not self.__locked
        assert self.samplewidth == other.samplewidth
        assert self.samplerate == other.samplerate
        assert self.nchannels == other.nchannels
        self.__frames += other.__frames
        return self

    def fadeout(self, seconds, target_volume=0.0):
        """Fade the end of the sample out to the target volume (usually zero) in the given time."""
        assert not self.__locked
        faded = Sample.get_array(self.__samplewidth)
        seconds = min(seconds, self.duration)
        i = self.frame_idx(self.duration-seconds)
        begin = self.__frames[:i]
        end = self.__frames[i:]  # we fade this chunk
        numsamples = len(end)/self.__samplewidth
        decrease = 1-target_volume
        for i in range(int(numsamples)):
            amplitude = 1-(i/numsamples)*decrease
            s = audioop.getsample(end, self.__samplewidth, i)
            faded.append(int(s*amplitude))
        end = faded.tobytes()
        if sys.byteorder == "big":
            end = audioop.byteswap(end, self.__samplewidth)
        self.__frames = begin + end
        return self

    def fadein(self, seconds, start_volume=0.0):
        """Fade the start of the sample in from the starting volume (usually zero) in the given time."""
        assert not self.__locked
        faded = Sample.get_array(self.__samplewidth)
        seconds = min(seconds, self.duration)
        i = self.frame_idx(seconds)
        begin = self.__frames[:i]  # we fade this chunk
        end = self.__frames[i:]
        numsamples = len(begin)/self.__samplewidth
        increase = 1-start_volume
        for i in range(int(numsamples)):
            amplitude = i*increase/numsamples+start_volume
            s = audioop.getsample(begin, self.__samplewidth, i)
            faded.append(int(s*amplitude))
        begin = faded.tobytes()
        if sys.byteorder == "big":
            begin = audioop.byteswap(begin, self.__samplewidth)
        self.__frames = begin + end
        return self

    def modulate_amp(self, modulator):
        """
        Perform amplitude modulation by another waveform or oscillator.
        You can use a Sample (or array of sample values) or an oscillator as modulator.
        If you use a Sample (or array), it will be cycled if needed and its maximum amplitude
        is scaled to be 1.0, effectively using it as if it was an oscillator.
        """
        assert not self.__locked
        frames = self.get_frame_array()
        if isinstance(modulator, (Sample, list, array.array)):
            # modulator is a waveform, turn that into an 'oscillator' ran
            if isinstance(modulator, Sample):
                modulator = modulator.get_frame_array()
            biggest = max(max(modulator), abs(min(modulator)))
            modulator = (v/biggest for v in itertools.cycle(modulator))
        else:
            modulator = iter(modulator)
        for i in range(len(frames)):
            frames[i] = int(frames[i] * next(modulator))
        self.__frames = frames.tobytes()
        if sys.byteorder == "big":
            self.__frames = audioop.byteswap(self.__frames, self.__samplewidth)
        return self

    def reverse(self):
        """Reverse the sound."""
        assert not self.__locked
        self.__frames = audioop.reverse(self.__frames, self.__samplewidth)
        return self

    def invert(self):
        """Invert every sample value around 0."""
        assert not self.__locked
        return self.amplify(-1)

    def delay(self, seconds, keep_length=False):
        """
        Delay the sample for a given time (inserts silence).
        If delay<0, instead, skip a bit from the start.
        This is a nice wrapper around the add_silence and clip functions.
        """
        assert not self.__locked
        if seconds > 0:
            if keep_length:
                num_frames = len(self.__frames)
                self.add_silence(seconds, at_start=True)
                self.__frames = self.__frames[:num_frames]
                return self
            else:
                return self.add_silence(seconds, at_start=True)
        elif seconds < 0:
            seconds = -seconds
            if keep_length:
                num_frames = len(self.__frames)
                self.add_silence(seconds)
                self.__frames = self.__frames[len(self.__frames)-num_frames:]
                return self
            else:
                self.__frames = self.__frames[self.frame_idx(seconds):]
        return self

    def bias(self, bias):
        """Add a bias constant to each sample value."""
        assert not self.__locked
        self.__frames = audioop.bias(self.__frames, self.__samplewidth, bias)
        return self

    def mono(self, left_factor=1.0, right_factor=1.0):
        """Make the sample mono (1-channel) applying the given left/right channel factors when downmixing"""
        assert not self.__locked
        if self.__nchannels == 1:
            return self
        if self.__nchannels == 2:
            self.__frames = audioop.tomono(self.__frames, self.__samplewidth, left_factor, right_factor)
            self.__nchannels = 1
            return self
        raise ValueError("sample must be stereo or mono already")

    def left(self):
        """Only keeps left channel."""
        assert not self.__locked
        assert self.__nchannels == 2
        return self.mono(1.0, 0)

    def right(self):
        """Only keeps right channel."""
        assert not self.__locked
        assert self.__nchannels == 2
        return self.mono(0, 1.0)

    def stereo(self, left_factor=1.0, right_factor=1.0):
        """
        Turn a mono sample into a stereo one with given factors/amplitudes for left and right channels.
        Note that it is a fast but simplistic conversion; the waveform in both channels is identical
        so you may suffer from phase cancellation when playing the resulting stereo sample.
        If the sample is already stereo, the left/right channel separation is changed instead.
        """
        assert not self.__locked
        if self.__nchannels == 2:
            # first split the left and right channels and then remix them
            right = self.copy().right()
            self.left().amplify(left_factor)
            return self.stereo_mix(right, 'R', right_factor)
        if self.__nchannels == 1:
            self.__frames = audioop.tostereo(self.__frames, self.__samplewidth, left_factor, right_factor)
            self.__nchannels = 2
            return self
        raise ValueError("sample must be mono or stereo already")

    def stereo_mix(self, other, other_channel, other_mix_factor=1.0, mix_at=0.0, other_seconds=None):
        """
        Mixes another mono channel into the current sample as left or right channel.
        The current sample will be the other channel.
        If the current sample already was stereo, the new mono channel is mixed with the existing left or right channel.
        """
        assert not self.__locked
        assert other.__nchannels == 1
        assert other.__samplerate == self.__samplerate
        assert other.__samplewidth == self.__samplewidth
        assert other_channel in ('L', 'R')
        if self.__nchannels == 1:
            # turn self into stereo first
            if other_channel == 'L':
                self.stereo(left_factor=0, right_factor=1)
            else:
                self.stereo(left_factor=1, right_factor=0)
        # turn other sample into stereo and mix it efficiently
        other = other.copy()
        if other_channel == 'L':
            other = other.stereo(left_factor=other_mix_factor, right_factor=0)
        else:
            other = other.stereo(left_factor=0, right_factor=other_mix_factor)
        return self.mix_at(mix_at, other, other_seconds)

    def pan(self, panning=0, lfo=None):
        """
        Linear Stereo panning, -1 = full left, 1 = full right.
        If you provide a LFO that will be used for panning instead.
        """
        assert not self.__locked
        if not lfo:
            return self.stereo((1-panning)/2, (1+panning)/2)
        lfo = iter(lfo)
        if self.__nchannels == 2:
            right = self.copy().right().get_frame_array()
            left = self.copy().left().get_frame_array()
            stereo = self.get_frame_array()
            for i in range(len(right)):
                panning = next(lfo)
                left_s = left[i]*(1-panning)/2
                right_s = right[i]*(1+panning)/2
                stereo[i*2] = int(left_s)
                stereo[i*2+1] = int(right_s)
        else:
            mono = self.get_frame_array()
            stereo = mono+mono
            for i, sample in enumerate(mono):
                panning = next(lfo)
                stereo[i*2] = int(sample*(1-panning)/2)
                stereo[i*2+1] = int(sample*(1+panning)/2)
            self.__nchannels = 2
        self.__frames = Sample.from_array(stereo, self.__samplerate, 2).__frames
        return self

    def echo(self, length, amount, delay, decay):
        """
        Adds the given amount of echos into the end of the sample,
        using a given length of sample data (from the end of the sample).
        The decay is the factor with which each echo is decayed in volume (can be >1 to increase in volume instead).
        If you use a very short delay the echos blend into the sound and the effect is more like a reverb.
        """
        assert not self.__locked
        if amount > 0:
            length = max(0, self.duration - length)
            echo = self.copy()
            echo.__frames = self.__frames[self.frame_idx(length):]
            echo_amp = decay
            for _ in range(amount):
                if echo_amp < 1.0/(2**(8*self.__samplewidth-1)):
                    # avoid computing echos that you can't hear
                    break
                length += delay
                echo = echo.copy().amplify(echo_amp)
                self.mix_at(length, echo)
                echo_amp *= decay
        return self

    def envelope(self, attack, decay, sustainlevel, release):
        """Apply an ADSR volume envelope. A,D,R are in seconds, Sustainlevel is a factor."""
        assert not self.__locked
        assert attack >= 0 and decay >= 0 and release >= 0
        assert 0 <= sustainlevel <= 1
        D = self.split(attack)   # self = A
        S = D.split(decay)
        if sustainlevel < 1:
            S.amplify(sustainlevel)   # apply the sustain level to S now so that R gets it as well
        R = S.split(S.duration - release)
        if attack > 0:
            self.fadein(attack)
        if decay > 0:
            D.fadeout(decay, sustainlevel)
        if release > 0:
            R.fadeout(release)
        self.join(D).join(S).join(R)
        return self

    def mix(self, other, other_seconds=None, pad_shortest=True):
        """
        Mix another sample into the current sample.
        You can limit the length taken from the other sample.
        When pad_shortest is False, no sample length adjustment is done.
        """
        assert not self.__locked
        assert self.samplewidth == other.samplewidth
        assert self.samplerate == other.samplerate
        assert self.nchannels == other.nchannels
        frames1 = self.__frames
        if other_seconds:
            frames2 = other.__frames[:other.frame_idx(other_seconds)]
        else:
            frames2 = other.__frames
        if pad_shortest:
            if len(frames1) < len(frames2):
                frames1 += b"\0"*(len(frames2)-len(frames1))
            elif len(frames2) < len(frames1):
                frames2 += b"\0"*(len(frames1)-len(frames2))
        self.__frames = audioop.add(frames1, frames2, self.samplewidth)
        return self

    def mix_at(self, seconds, other, other_seconds=None):
        """
        Mix another sample into the current sample at a specific time point.
        You can limit the length taken from the other sample.
        """
        if seconds == 0.0:
            return self.mix(other, other_seconds)
        assert not self.__locked
        assert self.samplewidth == other.samplewidth
        assert self.samplerate == other.samplerate
        assert self.nchannels == other.nchannels
        start_frame_idx = self.frame_idx(seconds)
        if other_seconds:
            other_frames = other.__frames[:other.frame_idx(other_seconds)]
        else:
            other_frames = other.__frames
        # Mix the frames. Unfortunately audioop requires splitting and copying the sample data, which is slow.
        pre, to_mix, post = self._mix_split_frames(len(other_frames), start_frame_idx)
        self.__frames = None  # allow for garbage collection
        mixed = audioop.add(to_mix, other_frames, self.samplewidth)
        del to_mix  # more garbage collection
        self.__frames = self._mix_join_frames(pre, mixed, post)
        return self

    def _mix_join_frames(self, pre, mid, post):
        # warning: slow due to copying (but only significant when not streaming)
        return pre + mid + post

    def _mix_split_frames(self, other_frames_length, start_frame_idx):
        # warning: slow due to copying (but only significant when not streaming)
        self._mix_grow_if_needed(start_frame_idx, other_frames_length)
        pre = self.__frames[:start_frame_idx]
        to_mix = self.__frames[start_frame_idx:start_frame_idx + other_frames_length]
        post = self.__frames[start_frame_idx + other_frames_length:]
        return pre, to_mix, post

    def _mix_grow_if_needed(self, start_frame_idx, other_length):
        # warning: slow due to copying (but only significant when not streaming)
        required_length = start_frame_idx + other_length
        if required_length > len(self.__frames):
            # we need to extend the current sample buffer to make room for the mixed sample at the end
            self.__frames += b"\0" * (required_length - len(self.__frames))


class Output:
    """Plays samples to audio output device or streams them to a file."""

    class SoundOutputter(threading.Thread):
        """Sound outputter running in its own thread. Requires PyAudio."""
        def __init__(self, samplerate, samplewidth, nchannels, queuesize=100):
            super().__init__(name="soundoutputter", daemon=True)
            self.audio = pyaudio.PyAudio()
            self.stream = self.audio.open(
                format=self.pyaudio_format_from_width(samplewidth),
                channels=nchannels, rate=samplerate, output=True)
            self.queue = queue.Queue(maxsize=queuesize)

        def pyaudio_format_from_width(self, width):
            if width == 2:
                return pyaudio.paInt16
            elif width == 3:
                return pyaudio.paInt24
            elif width == 4:
                return pyaudio.paInt32
            else:
                raise ValueError("Invalid width: %d" % width)

        def run(self):
            while True:
                sample = self.queue.get()
                if not sample:
                    break
                sample.write_frames(self.stream)
            # time.sleep(self.stream.get_output_latency()+self.stream.get_input_latency()+0.001)

        def play_immediately(self, sample, continuous=False):
            sample.write_frames(self.stream)
            if not continuous:
                filler = b"\0"*sample.samplewidth*sample.nchannels*self.stream.get_write_available()
                self.stream.write(filler)
                # time.sleep(self.stream.get_output_latency()+self.stream.get_input_latency()+0.001)

        def add_to_queue(self, sample):
            self.queue.put(sample)

        _wipe_lock = threading.Lock()
        def wipe_queue(self):
            with self._wipe_lock:
                try:
                    while True:
                        self.queue.get(block=False)
                except queue.Empty:
                    pass

        def close(self):
            if self.stream:
                self.stream.close()
                self.stream = None
            if self.audio:
                self.audio.terminate()
                self.audio = None

    def __init__(self, samplerate=Sample.norm_samplerate, samplewidth=Sample.norm_samplewidth, nchannels=Sample.norm_nchannels, queuesize=100):
        self.samplerate = samplerate
        self.samplewidth = samplewidth
        self.nchannels = nchannels
        if pyaudio:
            self.outputter = Output.SoundOutputter(samplerate, samplewidth, nchannels, queuesize)
            self.outputter.start()
            self.supports_streaming = True
        else:
            self.outputter = None
            self.supports_streaming = False

    def __repr__(self):
        return "<Output at 0x{0:x}, {1:d} channels, {2:d} bits, rate {3:d}>"\
            .format(id(self), self.nchannels, 8*self.samplewidth, self.samplerate)

    @classmethod
    def for_sample(cls, sample):
        return cls(sample.samplerate, sample.samplewidth, sample.nchannels)

    def __enter__(self):
        return self

    def __exit__(self, xtype, value, traceback):
        self.close()

    def close(self):
        if self.outputter:
            self.outputter.add_to_queue(None)

    def play_sample(self, sample, async=False):
        """Play a single sample."""
        assert sample.samplewidth == self.samplewidth
        assert sample.samplerate == self.samplerate
        assert sample.nchannels == self.nchannels
        if self.outputter:
            if async:
                self.outputter.add_to_queue(sample)
            else:
                self.outputter.play_immediately(sample)
        else:
            # try to fallback to winsound (only works on windows)
            sample_file = "__temp_sample.wav"
            sample.write_wav(sample_file)
            winsound.PlaySound(sample_file, winsound.SND_FILENAME)
            os.remove(sample_file)

    def play_samples(self, samples, async=False):
        """Plays all the given samples immediately after each other, with no pauses."""
        if self.outputter:
            for s in self.normalized_samples(samples, 26000):
                if async:
                    self.outputter.add_to_queue(s)
                else:
                    self.outputter.play_immediately(s, True)
        else:
            # winsound doesn't cut it when playing many small sample files...
            raise RuntimeError("Sorry but pyaudio is not installed. You need it to play streaming audio output.")

    def normalized_samples(self, samples, global_amplification=26000):
        """Generator that produces samples normalized to 16 bit using a single amplification value for all."""
        for sample in samples:
            if sample.samplewidth != 2:
                # We can't use automatic global max amplitude because we're streaming
                # the samples individually. So use a fixed amplification value instead
                # that will be used to amplify all samples in stream by the same amount.
                sample = sample.amplify(global_amplification).make_16bit(False)
            if sample.nchannels == 1:
                sample.stereo()
            assert sample.nchannels == 2
            assert sample.samplerate == 44100
            assert sample.samplewidth == 2
            yield sample

    def stream_to_file(self, filename, samples):
        """Saves the samples after each other into one single output wav file."""
        samples = self.normalized_samples(samples, 26000)
        sample = next(samples)
        with Sample.wave_write_begin(filename, sample) as out:
            for sample in samples:
                Sample.wave_write_append(out, sample)
            Sample.wave_write_end(out)

    def wipe_queue(self):
        """Remove all pending samples to be played from the queue"""
        self.outputter.wipe_queue()


# noinspection PyAttributeOutsideInit
class LevelMeter:
    """
    Keeps track of sound level (measured on the decibel scale where 0 db=max level).
    It has state, because it keeps track of the peak levels as well over time.
    The peaks eventually decay slowly if the actual level is decreased.
    """
    def __init__(self, rms_mode=False, lowest=-60.0):
        """
        Creates a new Level meter.
        Rms mode means that instead of peak volume, RMS volume will be used.
        """
        assert -60.0 <= lowest < 0.0
        self._rms = rms_mode
        self._lowest = lowest
        self.reset()

    def reset(self):
        """Resets the meter to its initial state with zero level."""
        self.peak_left = self.peak_right = self._lowest
        self._peak_left_hold = self._peak_right_hold = 0.0
        self.level_left = self.level_right = 0.0
        self._time = 0.0

    def process(self, sample):
        """
        Process a sample and calculate new levels (Left/Right) and new peak levels.
        This works best if you use short sample fragments (say < 0.1 seconds).
        It will update the level meter's state, but for convenience also returns
        the left, peakleft, right, peakright levels as a tuple.
        """
        if self._rms:
            left, right = sample.level_db_rms
        else:
            left, right = sample.level_db_peak
        left = max(left, self._lowest)
        right = max(right, self._lowest)
        time = self._time + sample.duration
        if (time-self._peak_left_hold) > 0.4:
            self.peak_left -= sample.duration*30.0
        if left >= self.peak_left:
            self.peak_left = left
            self._peak_left_hold = time
        if (time-self._peak_right_hold) > 0.4:
            self.peak_right -= sample.duration*30.0
        if right >= self.peak_right:
            self.peak_right = right
            self._peak_right_hold = time
        self.level_left = left
        self.level_right = right
        self._time = time
        return left, self.peak_left, right, self.peak_right
