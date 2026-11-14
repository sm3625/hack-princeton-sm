# Sound classifier evaluation

Model `classifier.tflite` v2: int8 TFLite, 56,648 bytes (55.3 KB). Input 49x40 log-mel (16 kHz mono, 1 s window, 32 ms FFT, 20 ms hop, 40 mel bands).

All numbers below are on **held-out source recordings** (ESC-50 fold 5, UrbanSound8K folds 9-10), scored through `node.classifier.TFLiteClassifier`, i.e. the exact int8 model and feature code the node runs. Windows are 1 s with 0.5 s hop.

## Targets (SPEC 3.1)

| target | result | status |
|---|---|---|
| model size <= 100 KB | 55.3 KB | met |
| inference <= 20 ms on laptop | median 0.47 ms, p95 0.75 ms | met |
| chainsaw recall >= 90% | 81.4% of 70 windows (95% CI 70.8%-88.8%) | **NOT met** |
| background false-positive rate <= 5% | 4.5% of 9364 windows (95% CI 4.1%-5.0%) | met |

Recall and false-positive rate here are per window at plain argmax (no confidence threshold), the strictest reading. At the node's thresholds (chainsaw 0.70, gunshot 0.70, vehicle 0.70) the background false-positive rate is 3.4% and chainsaw precision is 11.8%. Latency is the whole `classify()` call (feature extraction 0.26 ms + int8 model), one thread, median of 500 calls.

## Per-class, per window (argmax)

| class | precision | recall | windows |
|---|---|---|---|
| background | 0.939 | 0.955 | 9364 |
| chainsaw | 0.093 | 0.814 | 70 |
| gunshot | 0.658 | 0.904 | 83 |
| vehicle | 0.960 | 0.431 | 1335 |

| true \ predicted | background | chainsaw | gunshot | vehicle |
|---|---|---|---|---|
| **background** | 8941 | 360 | 39 | 24 |
| **chainsaw** | 13 | 57 | 0 | 0 |
| **gunshot** | 8 | 0 | 75 | 0 |
| **vehicle** | 560 | 199 | 0 | 576 |

## Per-class, per window, at the node's thresholds (chainsaw 0.70, gunshot 0.70, vehicle 0.70; else background)

| class | precision | recall | windows |
|---|---|---|---|
| background | 0.924 | 0.966 | 9364 |
| chainsaw | 0.118 | 0.786 | 70 |
| gunshot | 0.758 | 0.867 | 83 |
| vehicle | 0.976 | 0.365 | 1335 |

| true \ predicted | background | chainsaw | gunshot | vehicle |
|---|---|---|---|---|
| **background** | 9044 | 285 | 23 | 12 |
| **chainsaw** | 15 | 55 | 0 | 0 |
| **gunshot** | 11 | 0 | 72 | 0 |
| **vehicle** | 722 | 126 | 0 | 487 |

## Per clip (mean probability over the clip's windows)

| class | precision | recall | clips |
|---|---|---|---|
| background | 0.939 | 0.964 | 1325 |
| chainsaw | 0.082 | 0.875 | 8 |
| gunshot | 0.967 | 0.937 | 63 |
| vehicle | 0.988 | 0.416 | 190 |

| true \ predicted | background | chainsaw | gunshot | vehicle |
|---|---|---|---|---|
| **background** | 1277 | 45 | 2 | 1 |
| **chainsaw** | 1 | 7 | 0 | 0 |
| **gunshot** | 4 | 0 | 59 | 0 |
| **vehicle** | 78 | 33 | 0 | 79 |

## Node debounce (3 of 5 windows at the class threshold; gunshot 2 of 5), per clip

| class | clips where an event fires | rate |
|---|---|---|
| background (any false event) | 45 / 1325 | 3.4% |
| chainsaw (correct event) | 8 / 8 | 100.0% |
| gunshot (correct event) | 9 / 63 | 14.3% |
| vehicle (correct event) | 72 / 190 | 37.9% |

Gunshots are single impulses that light up about two overlapping windows, and many UrbanSound8K gunshot slices are shorter than 1 s, so the node fires a gunshot on 2 of 5 windows instead of 3.

## Background false alarms by source category (argmax, per window)

| category | false-alarm rate | windows |
|---|---|---|
| train | 16.7% | 72 |
| insects | 16.7% | 72 |
| door_wood_creaks | 16.7% | 60 |
| air_conditioner | 16.1% | 1400 |
| cow | 15.7% | 70 |
| washing_machine | 13.9% | 72 |
| can_opening | 13.5% | 52 |
| helicopter | 12.5% | 72 |
| vacuum_cleaner | 8.3% | 72 |
| glass_breaking | 7.1% | 28 |
| snoring | 5.6% | 72 |
| airplane | 5.6% | 72 |
| door_wood_knock | 5.4% | 56 |
| footsteps | 4.5% | 66 |
| toilet_flush | 4.2% | 72 |
| clapping | 2.9% | 68 |
| street_music | 2.8% | 1400 |
| sheep | 2.8% | 72 |
| siren | 2.3% | 1185 |
| dog_bark | 1.6% | 1093 |
| laughing | 1.4% | 71 |
| church_bells | 1.4% | 72 |
| children_playing | 0.9% | 1381 |
| wind | 0.0% | 72 |
| thunderstorm | 0.0% | 72 |
| sea_waves | 0.0% | 72 |
| rain | 0.0% | 72 |
| keyboard_typing | 0.0% | 72 |
| hen | 0.0% | 72 |
| frog | 0.0% | 72 |
| crying_baby | 0.0% | 72 |
| crow | 0.0% | 72 |
| crickets | 0.0% | 72 |
| crackling_fire | 0.0% | 72 |
| clock_tick | 0.0% | 72 |
| clock_alarm | 0.0% | 72 |
| chirping_birds | 0.0% | 72 |
| pouring_water | 0.0% | 69 |
| breathing | 0.0% | 69 |
| mouse_click | 0.0% | 68 |
| brushing_teeth | 0.0% | 67 |
| dog | 0.0% | 63 |
| drinking_sipping | 0.0% | 61 |
| water_drops | 0.0% | 60 |
| rooster | 0.0% | 57 |
| cat | 0.0% | 55 |
| coughing | 0.0% | 50 |
| sneezing | 0.0% | 46 |
| pig | 0.0% | 41 |

## Sounds left out of training

Ambiguous human-activity sounds are neither a target nor background, so they were excluded from training and from the numbers above. What the model calls them:

| category | windows | predictions |
|---|---|---|
| car_horn | 265 | background 264, gunshot 1 |
| drilling | 1220 | background 848, chainsaw 370, gunshot 2 |
| fireworks | 71 | background 69, gunshot 2 |
| hand_saw | 71 | background 70, chainsaw 1 |
| jackhammer | 1152 | background 930, vehicle 171, chainsaw 51 |

## Quantization

Window accuracy: float32 Keras 88.6%, int8 TFLite 88.9%; the two agree on 99.2% of windows.

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
- Hard negatives: during training the model scores plain training-fold background windows every few epochs, and the source categories it mistakes for a target are sampled up to 6x more often.
- Class bias and thresholds: a separate run that also held out ESC-50 fold 4 / US8K fold 8 tuned per-class logit offsets (a point of background false alarms costs five points of chainsaw recall) and one confidence threshold per class (at most 1% of background windows per class, without giving up more than 15% of correct windows). The shipped model uses offsets [0.0, 1.5, -0.25, -3.0] and thresholds chainsaw 0.70, gunshot 0.70, vehicle 0.70. Neither was tuned on the test folds.
