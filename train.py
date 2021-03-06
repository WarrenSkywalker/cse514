import os
import yaml
import time
import shutil
import torch
import random
import argparse
import numpy as np
# import tensorflow
from torch.utils import data
from tqdm import tqdm

from ptsemseg.models import get_model
from ptsemseg.loss import get_loss_function
from ptsemseg.loader import get_loader
from ptsemseg.utils import get_logger
from ptsemseg.metrics import runningScore, averageMeter
from ptsemseg.augmentations import get_composed_augmentations
from ptsemseg.schedulers import get_scheduler
from ptsemseg.optimizers import get_optimizer

from tensorboardX import SummaryWriter


def train(cfg, writer, logger):

    # Setup seeds
    torch.manual_seed(cfg.get("seed", 1337))
    torch.cuda.manual_seed(cfg.get("seed", 1337))
    np.random.seed(cfg.get("seed", 1337))
    random.seed(cfg.get("seed", 1337))

    # Setup device
    # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device("cuda")
    #print(device)
    # Setup Augmentations
    augmentations = cfg["training"].get("augmentations", None)
    #print(augmentations)
    data_aug = get_composed_augmentations(augmentations)
    #print(data_aug)
    #print("mark 1")
    # Setup Dataloader
    data_loader = get_loader(cfg["data"]["dataset"])
    data_path = cfg["data"]["path"]
    #print("mark 2")
    t_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg["data"]["train_split"],
        img_size=(cfg["data"]["img_rows"], cfg["data"]["img_cols"]),
        augmentations=data_aug,
    )
    #print("mark 2.5")
    v_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg["data"]["val_split"],
        img_size=(cfg["data"]["img_rows"], cfg["data"]["img_cols"]),
    )
    #print("mark 3")
    n_classes = t_loader.n_classes
    trainloader = data.DataLoader(
        t_loader,
        batch_size=cfg["training"]["batch_size"],
        num_workers=cfg["training"]["n_workers"],
        shuffle=True,
    )

    valloader = data.DataLoader(
        v_loader, batch_size=cfg["training"]["batch_size"], num_workers=cfg["training"]["n_workers"]
    )

    # Setup Metrics
    running_metrics_val = runningScore(n_classes)
    #print("mark 4")
    # Setup Model
    model = get_model(cfg["model"], n_classes).to(device)

    model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))

    # Setup optimizer, lr_scheduler and loss function
    optimizer_cls = get_optimizer(cfg)
    optimizer_params = {k: v for k, v in cfg["training"]["optimizer"].items() if k != "name"}
    #print("mark 5")
    optimizer = optimizer_cls(model.parameters(), **optimizer_params)
    logger.info("Using optimizer {}".format(optimizer))

    scheduler = get_scheduler(optimizer, cfg["training"]["lr_schedule"])

    loss_fn = get_loss_function(cfg)
    logger.info("Using loss {}".format(loss_fn))
    #print("mark 6")
    start_iter = 0
    if cfg["training"]["resume"] is not None:
        if os.path.isfile(cfg["training"]["resume"]):
            logger.info(
                "Loading model and optimizer from checkpoint '{}'".format(cfg["training"]["resume"])
            )
            checkpoint = torch.load(cfg["training"]["resume"])
            model.load_state_dict(checkpoint["model_state"])
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            start_iter = checkpoint["epoch"]
            logger.info(
                "Loaded checkpoint '{}' (iter {})".format(
                    cfg["training"]["resume"], checkpoint["epoch"]
                )
            )
        else:
            logger.info("No checkpoint found at '{}'".format(cfg["training"]["resume"]))
    #print("mark 7")
    val_loss_meter = averageMeter()
    time_meter = averageMeter()

    best_iou = -100.0
    i = start_iter
    flag = True
    #print("mark 8")
    while i <= cfg["training"]["train_iters"] and flag:

        for (images, labels) in trainloader:
            #print(images.shape)
            i += 1
            start_ts = time.time()

            model.train()
            images = images.to(device)
            labels = labels.to(device)
            #print("mark 1")
            optimizer.zero_grad()
            #print("mark 2")
            outputs = model(images)   #problem!!!
            #print("mark 3")
            loss = loss_fn(input=outputs, target=labels)
            #print("mark 3.5")
            loss.backward()
            # optimizer.step()
            optimizer.step()
            #print("mark 4")

            time_meter.update(time.time() - start_ts)

            # if i % 183 == 0:
            if i % 1104 == 0:
                learning_rate = scheduler.get_last_lr()
                print(learning_rate)
                fmt_lr = "Iter [{:d}/{:d}] pre learning rate: {:.15e}"
                print_lr = fmt_lr.format(i + 1, cfg["training"]["train_iters"], learning_rate[-1])
                print(print_lr)
                logger.info(print_lr)
                writer.add_scalar("current learning rate", learning_rate, i + 1)



            if (i + 1) % cfg["training"]["print_interval"] == 0:
                fmt_str = "Iter [{:d}/{:d}]  Loss: {:.4f}  Time/Image: {:.4f}"
                print_str = fmt_str.format(
                    i + 1,
                    cfg["training"]["train_iters"],
                    loss.item(),
                    time_meter.avg / cfg["training"]["batch_size"],
                )

                print(print_str)

                logger.info(print_str)
                writer.add_scalar("loss/train_loss", loss.item(), i + 1)
                time_meter.reset()

            if (i + 1) % cfg["training"]["val_interval"] == 0 or (i + 1) == cfg["training"][
                "train_iters"
            ]:
                model.eval()
                with torch.no_grad():
                    for i_val, (images_val, labels_val) in tqdm(enumerate(valloader)):
                        images_val = images_val.to(device)
                        labels_val = labels_val.to(device)

                        outputs = model(images_val)
                        val_loss = loss_fn(input=outputs, target=labels_val)

                        pred = outputs.data.max(1)[1].cpu().numpy()
                        # print("pred is: ",pred.shape)
                        gt = labels_val.data.cpu().numpy()
                        # print("gt is: ", gt.shape)
                        running_metrics_val.update(gt, pred)   #problem!!!
                        val_loss_meter.update(val_loss.item())

                writer.add_scalar("loss/val_loss", val_loss_meter.avg, i + 1)
                logger.info("Iter %d Loss: %.4f" % (i + 1, val_loss_meter.avg))

                score, class_iou = running_metrics_val.get_scores()
                for k, v in score.items():
                    print(k, v)
                    logger.info("{}: {}".format(k, v))
                    writer.add_scalar("val_metrics/{}".format(k), v, i + 1)

                for k, v in class_iou.items():
                    logger.info("{}: {}".format(k, v))
                    writer.add_scalar("val_metrics/cls_{}".format(k), v, i + 1)

                val_loss_meter.reset()
                running_metrics_val.reset()

                if score["Mean IoU : \t"] >= best_iou:
                    best_iou = score["Mean IoU : \t"]
                    state = {
                        "epoch": i + 1,
                        "model_state": model.state_dict(),
                        "optimizer_state": optimizer.state_dict(),
                        "scheduler_state": scheduler.state_dict(),
                        "best_iou": best_iou,
                    }
                    save_path = os.path.join(
                        writer.file_writer.get_logdir(),
                        "{}_{}_best_model.pkl".format(cfg["model"]["arch"], cfg["data"]["dataset"]),
                    )
                    torch.save(state, save_path)

            if (i + 1) == cfg["training"]["train_iters"]:
                flag = False
                break
        print("current epoch ", i/1104)
        scheduler.step()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument(
        "--config",
        nargs="?",
        type=str,
        default="configs/resunet2_pascal.yml",
        help="Configuration file to use",
    )
    #print("mark 11")
    args = parser.parse_args()

    with open(args.config) as fp:
        cfg = yaml.load(fp)
    #print("mark 12")
    run_id = random.randint(1, 100000)
    logdir = os.path.join("runs", os.path.basename(args.config)[:-4], str(run_id))
    writer = SummaryWriter(log_dir=logdir)
    #print("mark 13")
    print("RUNDIR: {}".format(logdir))
    shutil.copy(args.config, logdir)
    #print("mark 14")
    logger = get_logger(logdir)
    # logger.propagate = False
    logger.info("Let the games begin")
    #print("mark 15")
    train(cfg, writer, logger)
