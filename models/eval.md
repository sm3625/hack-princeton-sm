# Sound classifier evaluation

Model `classifier.tflite` v1: int8 TFLite, 56,648 bytes (55.3 KB). Input 49x40 log-mel (16 kHz mono, 1 s window, 32 ms FFT, 20 ms hop, 40 mel bands).

All numbers below are on **held-out source recordings** (ESC-50 fold 5, UrbanSound8K folds 9-10), scored through `node.classifier.TFLiteClassifier`, i.e. the exact int8 model and feature code the node runs. Windows are 1 s with 0.5 s hop.

## Targets (SPEC 3.1)

| target | result | status |
|---|---|---|
| model size <= 100 KB | 55.3 KB | met |
| inference <= 20 ms on laptop | median 0.24 ms, p95 0.25 ms | met |
| chainsaw recall >= 90% | 92.9% of 70 windows (95% CI 84.3%-96.9%) | met |
| background false-positive rate <= 5% | 6.9% of 9364 windows (95% CI 6.4%-7.5%) | **NOT met** |

Recall and false-positive rate here are per window at plain argmax (no confidence threshold), the strictest reading. At the runtime's 0.8 threshold the background false-positive rate is 4.4%. Latency is the whole `classify()` call (feature extraction 0.11 ms + int8 model), one thread, median of 500 calls.

## Per-class, per window (argmax)

| class | precision | recall | windows |
|---|---|---|---|
| background | 0.951 | 0.931 | 9364 |
| chainsaw | 0.076 | 0.929 | 70 |
| gunshot | 0.610 | 0.867 | 83 |
| vehicle | 0.918 | 0.493 | 1335 |

| true \ predicted | background | chainsaw | gunshot | vehicle |
|---|---|---|---|---|
| **background** | 8714 | 545 | 46 | 59 |
| **chainsaw** | 5 | 65 | 0 | 0 |
| **gunshot** | 11 | 0 | 72 | 0 |
| **vehicle** | 436 | 241 | 0 | 658 |

## Per-class, per window, confidence >= 0.8 (else background)

| class | precision | recall | windows |
|---|---|---|---|
| background | 0.924 | 0.956 | 9364 |
| chainsaw | 0.111 | 0.929 | 70 |
| gunshot | 0.755 | 0.855 | 83 |
| vehicle | 0.965 | 0.347 | 1335 |

| true \ predicted | background | chainsaw | gunshot | vehicle |
|---|---|---|---|---|
| **background** | 8956 | 368 | 23 | 17 |
| **chainsaw** | 5 | 65 | 0 | 0 |
| **gunshot** | 12 | 0 | 71 | 0 |
| **vehicle** | 718 | 154 | 0 | 463 |

## Per clip (mean probability over the clip's windows)

| class | precision | recall | clips |
|---|---|---|---|
| background | 0.952 | 0.954 | 1325 |
| chainsaw | 0.079 | 1.000 | 8 |
| gunshot | 0.966 | 0.905 | 63 |
| vehicle | 0.949 | 0.489 | 190 |

| true \ predicted | background | chainsaw | gunshot | vehicle |
|---|---|---|---|---|
| **background** | 1264 | 54 | 2 | 5 |
| **chainsaw** | 0 | 8 | 0 | 0 |
| **gunshot** | 6 | 0 | 57 | 0 |
| **vehicle** | 58 | 39 | 0 | 93 |

## SPEC 3.2 debounce (>= 3 of 5 windows at >= 0.8), per clip

| class | clips where an event fires | rate |
|---|---|---|
| background (any false event) | 52 / 1325 | 3.9% |
| chainsaw (correct event) | 8 / 8 | 100.0% |
| gunshot (correct event) | 1 / 63 | 1.6% |
| vehicle (correct event) | 63 / 190 | 33.2% |

Gunshots are single impulses: one shot only spans about two overlapping windows, and many UrbanSound8K gunshot slices are shorter than 1 s, so a 3-of-5 rule rarely fires for them even when every window is classified correctly. The node runtime needs a separate rule for gunshots (e.g. one window at >= 0.9).

## Background false alarms by source category (argmax, per window)

| category | false-alarm rate | windows |
|---|---|---|
| washing_machine | 34.7% | 72 |
| insects | 23.6% | 72 |
| air_conditioner | 22.0% | 1400 |
| cow | 20.0% | 70 |
| train | 19.4% | 72 |
| wind | 18.1% | 72 |
| door_wood_creaks | 16.7% | 60 |
| can_opening | 15.4% | 52 |
| sheep | 13.9% | 72 |
| airplane | 13.9% | 72 |
| snoring | 12.5% | 72 |
| helicopter | 11.1% | 72 |
| glass_breaking | 10.7% | 28 |
| breathing | 10.1% | 69 |
| vacuum_cleaner | 9.7% | 72 |
| mouse_click | 5.9% | 68 |
| clapping | 5.9% | 68 |
| street_music | 5.8% | 1400 |
| pig | 4.9% | 41 |
| toilet_flush | 4.2% | 72 |
| crow | 4.2% | 72 |
| church_bells | 4.2% | 72 |
| door_wood_knock | 3.6% | 56 |
| drinking_sipping | 3.3% | 61 |
| footsteps | 3.0% | 66 |
| crackling_fire | 2.8% | 72 |
| dog_bark | 2.4% | 1093 |
| siren | 2.3% | 1185 |
| dog | 1.6% | 63 |
| children_playing | 1.5% | 1381 |
| laughing | 1.4% | 71 |
| thunderstorm | 1.4% | 72 |
| frog | 1.4% | 72 |
| crying_baby | 1.4% | 72 |
| sea_waves | 0.0% | 72 |
| rain | 0.0% | 72 |
| keyboard_typing | 0.0% | 72 |
| hen | 0.0% | 72 |
| crickets | 0.0% | 72 |
| clock_tick | 0.0% | 72 |
| clock_alarm | 0.0% | 72 |
| chirping_birds | 0.0% | 72 |
| pouring_water | 0.0% | 69 |
| brushing_teeth | 0.0% | 67 |
| water_drops | 0.0% | 60 |
| rooster | 0.0% | 57 |
| cat | 0.0% | 55 |
| coughing | 0.0% | 50 |
| sneezing | 0.0% | 46 |

## Sounds left out of training

Ambiguous human-activity sounds are neither a target nor background, so they were excluded from training and from the numbers above. What the model calls them:

| category | windows | predictions |
|---|---|---|
| car_horn | 265 | background 257, chainsaw 5, gunshot 3 |
| drilling | 1220 | background 732, chainsaw 464, vehicle 22, gunshot 2 |
| fireworks | 71 | background 65, gunshot 5, chainsaw 1 |
| hand_saw | 71 | background 68, chainsaw 3 |
| jackhammer | 1152 | background 893, vehicle 224, chainsaw 35 |

## Quantization

Window accuracy: float32 Keras 87.7%, int8 TFLite 87.6%; the two agree on 99.5% of windows.

## Test set

| class | windows | clips |
|---|---|---|
| background | 9364 | 1325 |
| chainsaw | 70 | 8 |
| gunshot | 83 | 63 |
| vehicle | 1335 | 190 |

Only 8 chainsaw clips exist in the held-out fold (ESC-50 has 40 in total), so the chainsaw recall has a wide confidence interval; windows from one clip are correlated, so the real interval is wider than the per-window Wilson interval shown.

## Data, labels and splits

- **ESC-50** (K. J. Piczak, 2015), github.com/karolpiczak/ESC-50. CC BY-NC 3.0 (ESC-10 subset CC BY). 2000 x 5 s clips, 50 classes, 5 folds.
- **UrbanSound8K** (J. Salamon, C. Jacoby, J. P. Bello, 2014), Zenodo record 1203745. CC BY-NC 4.0. 8732 clips <= 4 s, 10 classes, 10 folds.
- Both licenses are non-commercial; the trained model is derived from this data.
- Labels: chainsaw = ESC-50 chainsaw. gunshot = US8K gun_shot. vehicle = ESC-50 engine + US8K engine_idling. background = every other ESC-50 class (rain, wind, birds, insects, crickets, frogs, thunderstorm, laughing, crying baby, coughing, footsteps, helicopter, airplane, train, vacuum cleaner, ...) + US8K air_conditioner, children_playing, dog_bark, siren, street_music. Excluded: ESC-50 hand_saw, fireworks, car_horn; US8K car_horn, drilling, jackhammer.
- Splits are by the datasets' own folds, which keep all clips cut from one source recording together. Train: ESC-50 folds 1-4, US8K folds 1-8. Test: ESC-50 fold 5, US8K folds 9-10.
- Target-class windows must contain the event (a 20 ms frame within 15 dB of the clip's loudest), so the silence ESC-50 pads clips with isn't labelled chainsaw. Windows of digital silence (< -80 dBFS RMS) are skipped in evaluation because the classifier short-circuits them to background.
- Training balances classes by sampling (background 3 : 1 : 1 : 1) and, within a class, by source category; augmentation mixes in background at 3-25 dB SNR, changes engine speed 0.8-1.25x (chainsaw/vehicle), low-pass filters (distance), adds a noise floor, and applies frequency/time masks. Adam with weight EMA; the final weights are used (no checkpoint picked on test or validation).
- Class bias: a separate run that also held out ESC-50 fold 4 / US8K fold 8 tuned per-class logit offsets (best chainsaw recall with background false positives <= 4% on those validation folds). The shipped model adds the same offsets [0.0, 2.5, -0.5, -2.0] to its output layer. They were not tuned on the test folds. On test they pushed false positives above target (above all air conditioner, washing machine, insects and wind called chainsaw), so chainsaw precision is low.
