import argparse
import json
import os
from pathlib import Path

import torch
from tqdm import tqdm

import hw_asr.model as module_model
from hw_asr.trainer import Trainer
from hw_asr.utils import ROOT_PATH
from hw_asr.utils.object_loading import get_dataloaders
from hw_asr.utils.parse_config import ConfigParser
from hw_asr.metric.utils import calc_cer, calc_wer

DEFAULT_CHECKPOINT_PATH = ROOT_PATH / "default_test_model" / "checkpoint.pth"


def main(config, out_file):
    logger = config.get_logger("test")

    # define cpu or gpu if possible
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # text_encoder
    text_encoder = config.get_text_encoder()

    # setup data_loader instances
    dataloaders = get_dataloaders(config, text_encoder)

    # build model architecture
    model = config.init_obj(config["arch"], module_model, n_class=len(text_encoder))
    logger.info(model)

    logger.info("Loading checkpoint: {} ...".format(config.resume))
    checkpoint = torch.load(config.resume, map_location=device)
    state_dict = checkpoint["state_dict"]
    if config["n_gpu"] > 1:
        model = torch.nn.DataParallel(model)
    model.load_state_dict(state_dict)

    # prepare model for testing
    model = model.to(device)
    model.eval()

    results = []

    with torch.no_grad():
        for batch_num, batch in enumerate(tqdm(dataloaders["test"])):
            batch = Trainer.move_batch_to_device(batch, device)
            output = model(**batch)
            if type(output) is dict:
                batch.update(output)
            else:
                batch["logits"] = output
            batch["log_probs"] = torch.log_softmax(batch["logits"], dim=-1)
            batch["log_probs_length"] = model.transform_input_lengths(
                batch["spectrogram_length"]
            )
            batch["probs"] = batch["log_probs"].exp().cpu()
            batch["argmax"] = batch["probs"].argmax(-1)
            for i in range(len(batch["text"])):
                argmax = batch["argmax"][i]
                argmax = argmax[: int(batch["log_probs_length"][i])]
                results.append(
                    {
                        "ground_truth": batch["text"][i],
                        "pred_text_argmax": text_encoder.ctc_decode(argmax.cpu().numpy()),
                        "pred_text_beam_search": text_encoder.ctc_beam_search(
                            batch["probs"][i][:batch["log_probs_length"][i]], beam_size=100
                        )[:10],
                        "pred_text_beam_search_lm": text_encoder.ctc_beam_search_lm(
                            batch["probs"][i][:batch["log_probs_length"][i]], beam_size=100
                        )[:10]
                    }
                )
    
    wer_argmax = []
    wer_bs = []
    wer_lm = []
    cer_argmax = []
    cer_bs = []
    cer_lm = []
    for el in results:
        gt = el["ground_truth"]
        bs = el['pred_text_beam_search'][0].text
        am = el['pred_text_argmax']
        lm = el['pred_text_beam_search_lm'][0].text
        wer_argmax.append(calc_wer(gt, am))
        wer_bs.append(calc_wer(gt, bs))
        wer_lm.append(calc_wer(gt, lm))
        cer_argmax.append(calc_cer(gt, am))
        cer_bs.append(calc_cer(gt, bs))
        cer_lm.append(calc_cer(gt, lm))

    def mean(arr):
        return sum(arr) / (1e-9 + len(arr))

    print(f"WER (argmax):\t\t\t{mean(wer_argmax)*100:.2f}%")
    print(f"WER (beam search):\t\t\t{mean(wer_bs)*100:.2f}%")
    print(f"WER (beam search + LM):\t\t{mean(wer_lm)*100:.2f}%")
    print(f"CER (argmax):\t\t{mean(cer_argmax)*100:.2f}%")
    print(f"CER (beam search):\t\t\t{mean(cer_bs)*100:.2f}%")
    print(f"CER (beam search + LM):\t\t{mean(cer_lm)*100:.2f}%")

    with Path(out_file).open("w") as f:
        json.dump(results, f, indent=2)
    
    return mean(cer_lm)


if __name__ == "__main__":
    args = argparse.ArgumentParser(description="PyTorch Template")
    args.add_argument(
        "-c",
        "--config",
        default=None,
        type=str,
        help="config file path (default: None)",
    )
    args.add_argument(
        "-r",
        "--resume",
        default=str(DEFAULT_CHECKPOINT_PATH.absolute().resolve()),
        type=str,
        help="path to latest checkpoint (default: None)",
    )
    args.add_argument(
        "-d",
        "--device",
        default=None,
        type=str,
        help="indices of GPUs to enable (default: all)",
    )
    args.add_argument(
        "-o",
        "--output",
        default="output.json",
        type=str,
        help="File to write results (.json)",
    )
    args.add_argument(
        "-t",
        "--test-data-folder",
        default=None,
        type=str,
        help="Path to dataset",
    )
    args.add_argument(
        "-b",
        "--batch-size",
        default=20,
        type=int,
        help="Test dataset batch size",
    )
    args.add_argument(
        "-j",
        "--jobs",
        default=1,
        type=int,
        help="Number of workers for test dataloader",
    )    
    args.add_argument(
        "--alpha",
        default=None,
        type=float,
        help="Alpha for beam search",
    )
    args.add_argument(
        "--beta",
        default=None,
        type=float,
        help="Beta for beam search",
    )

    args = args.parse_args()

    # set GPUs
    if args.device is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = args.device

    # first, we need to obtain config with model parameters
    # we assume it is located with checkpoint in the same folder
    model_config = Path(args.resume).parent / "config.json"
    with model_config.open() as f:
        config = ConfigParser(json.load(f), resume=args.resume)

    # update with addition configs from `args.config` if provided
    if args.config is not None:
        with Path(args.config).open() as f:
            config.config.update(json.load(f))

    # if `--test-data-folder` was provided, set it as a default test set
    if args.test_data_folder is not None:
        test_data_folder = Path(args.test_data_folder).absolute().resolve()
        assert test_data_folder.exists()
        config.config["data"] = {
            "test": {
                "batch_size": args.batch_size,
                "num_workers": args.jobs,
                "datasets": [
                    {
                        "type": "CustomDirAudioDataset",
                        "args": {
                            "audio_dir": str(test_data_folder / "audio"),
                            "transcription_dir": str(
                                test_data_folder / "transcriptions"
                            ),
                        },
                    }
                ],
            }
        }

    assert config.config.get("data", {}).get("test", None) is not None
    config["data"]["test"]["batch_size"] = args.batch_size
    config["data"]["test"]["n_jobs"] = args.jobs


    '''
    from tqdm import tqdm
    best_cer = 100
    best_alpha = None
    best_beta = None
    for alpha in [0.0, 0.001, 0.01, 0.1, 0.5, 1.0, 1.5, 2.0]:
        for beta in tqdm([0.0, 0.001, 0.01, 0.1, 0.5, 1.0, 1.5, 2.0]):
            config["text_encoder"]["args"]["alpha"] = alpha
            config["text_encoder"]["args"]["beta"] = beta
            cer = main(config, args.output)
            if cer < best_cer:
                best_cer = cer
                best_alpha = alpha
                best_beta = beta
    print(best_cer, best_alpha, best_beta)
    exit(0)
    '''

    if args.alpha is not None:
        config["text_encoder"]["args"]["alpha"] = args.alpha
    if args.beta is not None:
        config["text_encoder"]["args"]["beta"] = args.beta

    main(config, args.output)
