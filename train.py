import os
import time
import argparse
import numpy as np
from datetime import datetime

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.tensorboard import SummaryWriter
import wandb

from data_reader import get_kfold_data_loaders, CoffeeDataset
from utils import check_set_gpu, save_checkpoint, plot_training_history, log_metrics_to_wandb, log_metrics_to_tensorboard
from get_model import get_model
from validate import validate
from anova_test import perform_anova

def train_one_epoch(model, train_loader, criterion, optimizer, epoch, device):
    """Train the model for one epoch."""
    model.train()
    running_loss = 0.0
    correct = 0
    total = 0
    
    for i, (inputs, labels) in enumerate(train_loader):
        inputs, labels = inputs.to(device), labels.to(device)
        
        # Zero the parameter gradients
        optimizer.zero_grad()
        
        # Forward + backward + optimize
        outputs = model(inputs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        
        # Statistics
        running_loss += loss.item()
        _, predicted = outputs.max(1)
        total += labels.size(0)
        correct += predicted.eq(labels).sum().item()
        
        if (i + 1) % 10 == 0:  # Print every 10 mini-batches
            print(f'Epoch: {epoch + 1}, Batch: {i + 1}/{len(train_loader)}, '
                  f'Loss: {running_loss / (i + 1):.4f}, '
                  f'Acc: {100. * correct / total:.2f}%')
    
    epoch_loss = running_loss / len(train_loader)
    epoch_acc = 100. * correct / total
    return epoch_loss, epoch_acc

def train(model_name, batch_size=32, lr=0.00001, epochs=100, patience=5, device_override=None, use_wandb=True):
    """Main training function with K-Fold cross-validation."""
    # Generate timestamp for run name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    run_name = f"{model_name}_{timestamp}"
    
    # Initialize wandb if enabled
    writer = None
    if use_wandb:
        wandb.init(
            project="CoffeeBeanDefectClassification",
            name=run_name,
            config={
                "model": model_name,
                "batch_size": batch_size,
                "learning_rate": lr,
                "epochs": epochs,
                "patience": patience
            }
        )
    else:
        log_dir = os.path.join("runs", run_name)
        writer = SummaryWriter(log_dir=log_dir)
    
    # Set device using the utility function
    device = check_set_gpu(device_override)
    
    # Create output directory
    os.makedirs('models', exist_ok=True)
    
    # Get full dataset
    full_dataset = CoffeeDataset()
    
    # Get K-Fold data loaders
    fold_loaders = get_kfold_data_loaders(full_dataset, batch_size=batch_size)
    num_classes = len(full_dataset.classes)
    print(f"Classes: {full_dataset.classes}")
    print(f"Number of classes: {num_classes}")
    
    # Store results for each fold
    fold_results = []
    avg_val_f1_list = []
    avg_val_recall_list = []
    avg_val_precision_list = []
    
    # Iterate through each fold
    for fold, (train_loader, val_loader) in enumerate(fold_loaders):
        print(f"\nStarting Fold {fold + 1}/{len(fold_loaders)}")

        # Get model
        model = get_model(model_name, num_classes)
        model = model.to(device)

        # Log model architecture to TensorBoard
        if use_wandb:
            wandb.watch(model, log="all")
        else:
            example_input = next(iter(train_loader))[0][0].unsqueeze(0).to(device)
            writer.add_graph(model, example_input)

        # Loss function and optimizer
        criterion = nn.CrossEntropyLoss()
        optimizer = optim.Adam(model.parameters(), lr=lr)
        scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3)

        print(f"Initial Learning Rate: {optimizer.param_groups[0]['lr']}")

        # Initialize variables
        best_val_acc = 0.0
        epochs_no_improve = 0
        early_stop = False

        # History for plotting
        train_losses, val_losses = [], []
        train_accs, val_accs = [], []
        val_f1s, val_precisions = [], []
        val_recalls = []

        start_time = time.time()
        for epoch in range(epochs):
            if early_stop:
                print("Early stopping triggered!")
                break

            print(f"\nEpoch {epoch + 1}/{epochs}")
            print("-" * 20)

            # Train and validate - pass device to the functions
            train_loss, train_acc = train_one_epoch(model, train_loader, criterion, optimizer, epoch, device)
            val_loss, val_acc, val_precision, val_recall, val_f1 = validate(model, val_loader, criterion, device)

            # Update learning rate
            scheduler.step(val_loss)

            # Save history
            train_losses.append(train_loss)
            val_losses.append(val_loss)
            train_accs.append(train_acc)
            val_accs.append(val_acc)
            val_f1s.append(val_f1) 
            val_precisions.append(val_precision) 
            val_recalls.append(val_recall) 

            # Log metrics to wandb if enabled
            log_metrics_to_wandb(fold, train_loss, train_acc, val_loss, val_acc, val_precision, val_recall, val_f1, optimizer.param_groups[0]['lr'], epoch, use_wandb)

            if not use_wandb:
                log_metrics_to_tensorboard(writer, fold, train_loss, train_acc, val_loss, val_acc, val_precision, val_recall, val_f1, optimizer.param_groups[0]['lr'], epoch)

            # Print epoch summary
            print(f"Train Loss: {train_loss:.4f}, Train Acc: {train_acc:.2f}%")
            print(f"Val Loss: {val_loss:.4f}, Val Acc: {val_acc:.2f}%")
            print(f"Val Precision: {val_precision:.4f}, Val Recall: {val_recall:.4f}, Val F1: {val_f1:.4f}")

            # Save model if validation accuracy improves
            if val_acc > best_val_acc:
                print(f"Validation accuracy improved from {best_val_acc:.2f}% to {val_acc:.2f}%")
                best_val_acc = val_acc
                checkpoint_path = f"models/{run_name}_fold{fold + 1}_best.pth"
                save_checkpoint(model, optimizer, epoch, val_acc, checkpoint_path)

                # Save best model to wandb if enabled
                if use_wandb:
                    wandb.save(checkpoint_path)

                epochs_no_improve = 0
            else:
                epochs_no_improve += 1
                print(f"No improvement for {epochs_no_improve} epochs")
    
            # Early stopping
            if epochs_no_improve >= patience:
                print(f"Early stopping triggered after {patience} epochs without improvement")
                early_stop = True

        # Print fold summary
        training_time = time.time() - start_time
        print(f"Fold {fold + 1} complete in {training_time:.2f}s")
        print(f"Best validation accuracy: {best_val_acc:.2f}%")

        avg_val_f1 = np.mean(val_f1s)
        avg_val_f1_list.append(avg_val_f1)
        avg_val_recall = np.mean(val_recalls)
        avg_val_recall_list.append(avg_val_recall)
        avg_val_precision = np.mean(val_precisions)
        avg_val_precision_list.append(avg_val_precision)

        # Save fold results
        fold_results.append({
            "fold": fold + 1,
            "best_val_acc": best_val_acc,
            "train_losses": train_losses,
            "val_losses": val_losses,
            "train_accs": train_accs,
            "val_accs": val_accs,
            "val_f1s": val_f1s,
            "val_precisions": val_precisions,
            "val_recalls": val_recalls
        })

        # Save final model for this fold
        final_model_path = f"models/{run_name}_fold{fold + 1}_final.pth"
        save_checkpoint(model, optimizer, epoch, val_acc, final_model_path)
        if use_wandb:
            wandb.save(final_model_path)

    # Calculate average validation accuracy across all folds
    avg_val_acc = np.mean([result["best_val_acc"] for result in fold_results])
    print(f"\nAverage validation accuracy across all folds: {avg_val_acc:.2f}%")

    # Calculate average f1 score across all folds
    avg_val_f1 = np.mean(avg_val_f1_list)
    print(f"\nAverage validation F1 Score across all folds: {avg_val_f1:.4f}")

    # Calculate average recall across all folds
    avg_val_recall = np.mean(avg_val_recall_list)
    print(f"\nAverage validation Recall across all folds: {avg_val_recall:.4f}")

    # Calculate average precision across all folds
    avg_val_precision = np.mean(avg_val_precision_list)
    print(f"\nAverage validation Precision across all folds: {avg_val_precision:.4f}")
    
    if use_wandb:
        wandb.log({
            "average_val_acc": avg_val_acc,
            "average_val_f1": avg_val_f1,
            "average_val_recall": avg_val_recall,
            "average_val_precision": avg_val_precision
        })
        wandb.finish()
    else:
        writer.close()
    
    return fold_results, avg_val_acc, avg_val_f1, avg_val_recall, avg_val_precision

def main():
    """Main function with argument parsing."""
    parser = argparse.ArgumentParser(description="Train coffee classification models")
    parser.add_argument("--model", type=str, choices=["efficientnet", "resnet50", "mobilenetv3", "densenet121", "vit", "convnext", "regnet"], 
                        default="efficientnet", help="Model architecture to use")
    parser.add_argument("--batch_size", type=int, default=32, help="Batch size for training")
    parser.add_argument("--lr", type=float, default=0.00001, help="Learning rate")
    parser.add_argument("--epochs", type=int, default=100, help="Number of epochs")
    parser.add_argument("--patience", type=int, default=10, help="Early stopping patience")
    parser.add_argument("--device", type=str, choices=["cuda", "mps", "cpu"], 
                        default=None, help="Device to use (overrides automatic detection)")
    parser.add_argument("--no-wandb", action="store_true", help="Disable wandb logging and use tensorboard")
    parser.add_argument("--all", action="store_true", help="Train all models")

    args = parser.parse_args()

    all_models = ["efficientnet", "resnet50", "mobilenetv3", "densenet121", "vit", "convnext", "regnet"]

    if args.all:
        print("Training all models...")
        results = {}
        accuracies = {model: [] for model in all_models}
        f1_scores = {model: [] for model in all_models}
        precisions = {model: [] for model in all_models}
        recalls = {model: [] for model in all_models}

        for model_name in all_models:
            if not args.no_wandb:
                wandb.init(
                    project="coffee-classification",
                    name=model_name,
                    config={
                        "batch_size": args.batch_size,
                        "learning_rate": args.lr,
                        "epochs": args.epochs,
                        "patience": args.patience
                    }
                )
            print(f"\nTraining {model_name} model")
            fold_results, avg_val_acc, avg_val_f1, avg_val_recall, avg_val_precision = train(
                model_name,
                args.batch_size,
                args.lr,
                args.epochs,
                args.patience,
                args.device,
                not args.no_wandb
            )
            results[model_name] = {
                "avg_val_acc": avg_val_acc,
                "avg_val_f1": avg_val_f1,
                "avg_val_recall": avg_val_recall,
                "avg_val_precision": avg_val_precision
            }
            accuracies[model_name] = [result["val_accs"] for result in fold_results]
            f1_scores[model_name] = [result["val_f1s"] for result in fold_results]
            precisions[model_name] = [result["val_precisions"] for result in fold_results]
            recalls[model_name] = [result["val_recalls"] for result in fold_results]
            print(f"{model_name} - Average Validation Accuracy: {avg_val_acc:.2f}%")
            print(f"{model_name} - Average Validation F1 Score: {avg_val_f1:.4f}")

        print("\nSummary of Results:")
        for model_name, metrics in results.items():
            print(f"{model_name}: Accuracy = {metrics['avg_val_acc']:.2f}%, F1 Score = {metrics['avg_val_f1']:.4f}, Precision = {np.mean(precisions[model_name]):.4f}, Recall = {np.mean(recalls[model_name]):.4f}")

        f_stat, p_value, anova_result = perform_anova(accuracies)
        print(f"\nANOVA Results:")
        print(f"F-statistic: {f_stat:.4f}, p-value: {p_value:.4f}")
        print(anova_result)

        if not args.no_wandb:
            wandb.log({
                "ANOVA/F-statistic": f_stat,
                "ANOVA/p-value": p_value,
                "ANOVA/Result": anova_result
            })
            for model_name in all_models:
                wandb.log({
                    f"{model_name}/avg_val_acc": results[model_name]["avg_val_acc"],
                    f"{model_name}/avg_val_f1": results[model_name]["avg_val_f1"],
                    f"{model_name}/avg_val_recall": results[model_name]["avg_val_recall"],
                    f"{model_name}/avg_val_precision": results[model_name]["avg_val_precision"]
                })

        if not args.no_wandb:
            wandb.finish()
    else:
        print(f"Training with {args.model} model")
        fold_results, avg_val_acc, avg_val_f1, avg_val_recall, avg_val_precision = train(
            args.model,
            args.batch_size,
            args.lr,
            args.epochs,
            args.patience,
            args.device,
            not args.no_wandb
        )
        print(f"Average Validation Accuracy: {avg_val_acc:.2f}%")
        print(f"Average Validation F1 Score: {avg_val_f1:.4f}")
        print(f"Average Validation Recall: {avg_val_recall:.4f}")
        print(f"Average Validation Precision: {avg_val_precision:.4f}")

if __name__ == "__main__":
    main()