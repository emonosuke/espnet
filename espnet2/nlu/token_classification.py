import torch

class LinearDecoder(torch.nn.Module):
    """Linear decoder for token classification"""

    def __init__(
        self,
        encoder_output_size: int,
        num_labels: int = 2,
    ):
        super().__init__()
        self._num_labels = num_labels
        self.linear_decoder = torch.nn.Linear(encoder_output_size, num_labels)

    def forward(self, input: torch.Tensor, ilens: torch.Tensor):
        """Forward.

        Args:
            input (torch.Tensor): hidden_space [Batch, T, F]
            ilens (torch.Tensor): input lengths [Batch]
        """

        output = self.linear_decoder(input)

        return output

    @property
    def num_labels(self):
        return self._num_labels