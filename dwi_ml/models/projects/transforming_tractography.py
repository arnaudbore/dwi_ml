# -*- coding: utf-8 -*-
import logging
from time import time
from typing import Union, List, Tuple

from dipy.data import get_sphere
import numpy as np
import torch
from torch.nn import Dropout, Transformer
from torch.nn.functional import pad

from dwi_ml.data.processing.streamlines.sos_eos_management import \
    add_label_as_last_dim, convert_dirs_to_class
from dwi_ml.data.processing.streamlines.post_processing import compute_directions
from dwi_ml.data.spheres import TorchSphere
from dwi_ml.models.embeddings_on_tensors import keys_to_embeddings
from dwi_ml.models.main_models import (MainModelOneInput,
                                       ModelWithDirectionGetter,
                                       ModelWithNeighborhood)
from dwi_ml.models.projects.positional_encoding import keys_to_positional_encodings
from dwi_ml.models.utils.transformers_from_torch import (
    ModifiedTransformer,
    ModifiedTransformerEncoder, ModifiedTransformerEncoderLayer,
    ModifiedTransformerDecoder, ModifiedTransformerDecoderLayer)

# Our model needs to be autoregressive, to allow inference / generation at
# tracking time.
# => During training, we hide the future; both in the input and in the target
# sequences.

# About the tracking process
# At each new step, the whole sequence is processed again (ran in the model).
# We only keep the last output. This is not very efficient... Is there a way
# to keep the hidden state in-between?
logger = logging.getLogger('model_logger')  # Same logger as Super.

# Trying to help with memory.
# When running out of memory, the error message is:
# torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate XX (GPU 0;
# X total capacity; X already allocated; X free; X reserved in total by Torch)
# If reserved memory is >> allocated memory try setting max_split_size_mb to
# avoid fragmentation. Value to which to limit is unclear.
# Tested, does not seem to improve much.
# os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:512"
CLEAR_CACHE = False


def forward_padding(data: torch.tensor, expected_length):
    return pad(data, (0, 0, 0, expected_length - len(data)))


def pad_and_stack_batch(data: List[torch.Tensor], pad_first: bool,
                        pad_length: int):
    """
    Pad the list of tensors so that all streamlines have length max_len.
    Then concatenate all streamlines.

    Params
    ------
    data: list[Tensor]
        Len: nb streamlines. Shape of each tensor: nb points x nb features.
    pad_first: bool
        If false, padding is skipped. (Ex: If all streamlines already
        contain the right number of points.)
    pad_length: int
        Expected final lengths of streamlines.

    Returns
    -------
    formatted_x: Tensor
        Shape [nb_streamlines, max_len, nb_features] where nb features is
        the size of the batch input at this point (ex, initial number of
        features or d_model if embedding is already done).
    """
    if pad_first:
        data = [forward_padding(data[i], pad_length) for i in range(len(data))]

    return torch.stack(data)


class AbstractTransformerModel(ModelWithNeighborhood, MainModelOneInput,
                               ModelWithDirectionGetter):
    """
    Prepares the parts common to our two transformer versions: embeddings,
    direction getter and some parameters for the model.

    Encoder and decoder will be prepared in child classes.

    About data embedding:
    We could use the raw data, technically. But when adding the positional
    embedding, the reason it works is that the learning of the embedding
    happens while knowing that some positional vector will be added to it.
    As stated in the blog
    https://kazemnejad.com/blog/transformer_architecture_positional_encoding/
    the embedding probably adapts to leave place for the positional encoding.
    """
    def __init__(self,
                 experiment_name: str,
                 step_size: Union[float, None], compress_lines: Union[float, None],
                 # INPUTS IN ENCODER
                 nb_features: int, embedding_key_x: str, embedding_size_x: int,
                 # TARGETS IN DECODER
                 token_type: str, embedding_key_t: str, embedding_size_t: int,
                 # GENERAL TRANSFORMER PARAMS
                 max_len: int, positional_encoding_key: str,
                 d_model: int, ffnn_hidden_size: Union[int, None],
                 nheads: int, dropout_rate: float, activation: str,
                 norm_first: bool, start_from_copy_prev: bool,
                 # DIRECTION GETTER
                 dg_key: str, dg_args: dict,
                 # Other
                 neighborhood_type: Union[str, None],
                 neighborhood_radius: Union[int, float, List[float], None],
                 log_level=logging.root.level):
        """
        Args
        ----
        nb_features: int
            This value should be known from the actual data. Number of features
            in the data (last dimension).
        embedding_key_x: str,
            Chosen class for the input embedding (the data embedding part).
            Choices: keys_to_embeddings.keys().
            Default: 'no_embedding'.
        embedding_size_x: int
            Embedding size for x. In the base model, must be d_model.
        token_type: str
            Either 'as_label' or the name of the sphere to convert to classes.
            Used for SOS addition.
        embedding_key_t: str,
            Target embedding, with the same choices as above.
            Default: 'no_embedding'.
        embedding_size_t: int
            Embedding size for t. In the base model, must be d_model.
        max_len: int
            Maximal sequence length. This is only used in the positional
            encoding. During the forward call, batches are only padded to the
            longest sequence in the batch. However, positional encoding only
            makes sence if not streamlines are longer than that value (this is
            verified).
        positional_encoding_key: str,
            Chosen class for the input's positional embedding. Choices:
            keys_to_positional_embeddings.keys(). Default: 'sinusoidal'.
        d_model: int,
            The transformer REQUIRES the same output dimension for each layer
            everywhere to allow skip connections. = d_model. Note that
            embeddings should also produce outputs of size d_model.
            Value must be divisible by num_heads.
            Default: 4096.
        ffnn_hidden_size: int
            Size of the feed-forward neural network (FFNN) layer in the encoder
            and decoder layers. The FFNN is composed of two linear layers. This
            is the size of the output of the first one. In the music paper,
            = d_model/2. Default: d_model/2.
        nheads: int
            Number of attention heads in each attention or self-attention
            layer. Default: 8.
        dropout_rate: float
            Dropout rate. Constant in every dropout layer. Default: 0.1.
        activation: str
            Choice of activation function in the FFNN. 'relu' or 'gelu'.
            Default: 'relu'.
        norm_first: bool
            If True, encoder and decoder layers will perform LayerNorms before
            other attention and feedforward operations, otherwise after.
            Torch default + in original paper: False. In the tensor2tensor code
            they suggest that learning is more robust when preprocessing each
            layer with the norm.
        """
        super().__init__(
            # MainAbstract
            experiment_name=experiment_name, step_size=step_size,
            compress_lines=compress_lines, log_level=log_level,
            # Neighborhood
            neighborhood_type=neighborhood_type,
            neighborhood_radius=neighborhood_radius,
            # Tracking
            dg_key=dg_key, dg_args=dg_args)

        self.nb_features = nb_features
        self.embedding_key_x = embedding_key_x
        self.token_type = token_type
        self.embedding_key_t = embedding_key_t
        self.max_len = max_len
        self.positional_encoding_key = positional_encoding_key
        self.d_model = d_model
        self.embedding_size_x = embedding_size_x
        self.embedding_size_t = embedding_size_t
        self.nheads = nheads
        self.ffnn_hidden_size = ffnn_hidden_size if ffnn_hidden_size \
            else d_model // 2
        self.dropout_rate = dropout_rate
        self.activation = activation
        self.norm_first = norm_first
        self.start_from_copy_prev = start_from_copy_prev

        # ----------- Checks
        assert embedding_size_x > 3, \
            "Current computation of the positional encoding required data " \
            "of size > 3, but got {}".format(embedding_size_x)
        if self.embedding_key_x not in keys_to_embeddings.keys():
            raise ValueError("Embedding choice for x data not understood: {}"
                             .format(self.embedding_key_x))
        if self.embedding_key_t not in keys_to_embeddings.keys():
            raise ValueError("Embedding choice for targets not understood: {}"
                             .format(self.embedding_key_t))
        if self.positional_encoding_key not in \
                keys_to_positional_encodings.keys():
            raise ValueError("Positional encoding choice not understood: {}"
                             .format(self.positional_encoding_key))
        assert d_model // nheads == float(d_model) / nheads, \
            "d_model ({}) must be divisible by nheads ({})"\
            .format(d_model, nheads)

        # ----------- Instantiations
        # This dropout is only used in the embedding; torch's transformer
        # prepares its own dropout elsewhere, and direction getter too.
        self.dropout = Dropout(self.dropout_rate)

        # 1. x embedding
        input_size = nb_features * (self.nb_neighbors + 1)
        cls_x = keys_to_embeddings[self.embedding_key_x]
        self.embedding_layer_x = cls_x(input_size, self.embedding_size_x)

        # 2. positional encoding
        cls_p = keys_to_positional_encodings[self.positional_encoding_key]
        self.position_encoding_layer = cls_p(embedding_size_x, dropout_rate,
                                             max_len)

        # 3. target embedding
        cls_t = keys_to_embeddings[self.embedding_key_t]
        if token_type == 'as_label':
            self.token_sphere = None
            target_features = 4
        else:
            dipy_sphere = get_sphere(token_type)
            self.token_sphere = TorchSphere(dipy_sphere)
            # nb classes = nb_vertices + SOS
            target_features = len(self.token_sphere.vertices) + 1
        self.embedding_layer_t = cls_t(target_features, embedding_size_t)

        # 4. Transformer: See child classes

        # 5. Direction getter
        # Original paper: last layer = Linear + Softmax on nb of classes.
        # Note on parameter initialization.
        # They all use torch.nn.linear, which initializes parameters based
        # on a kaiming uniform, same as uniform(-sqrt(k), sqrt(k)) where k is
        # the nb of features.
        self.instantiate_direction_getter(d_model)

        assert self.loss_uses_streamlines
        self.forward_uses_streamlines = True

    @property
    def params_for_checkpoint(self):
        """
        Every parameter necessary to build the different layers again from a
        checkpoint.
        """
        p = super().params_for_checkpoint
        p.update({
            'nb_features': int(self.nb_features),
            'embedding_key_x': self.embedding_key_x,
            'token_type': self.token_type,
            'embedding_key_t': self.embedding_key_t,
            'max_len': self.max_len,
            'positional_encoding_key': self.positional_encoding_key,
            'dropout_rate': self.dropout_rate,
            'activation': self.activation,
            'nheads': self.nheads,
            'ffnn_hidden_size': self.ffnn_hidden_size,
            'norm_first': self.norm_first,
            'start_from_copy_prev': self.start_from_copy_prev
        })

        return p

    def set_context(self, context):
        assert context in ['training', 'validation', 'tracking', 'visu']
        self._context = context

    def move_to(self, device):
        super().move_to(device)
        if self.token_sphere is not None:
            self.token_sphere.move_to(device)

    def _prepare_targets_forward(self, batch_streamlines):
        """
        batch_streamlines: List[Tensors]
        during_loss: bool
            If true, this is called during loss computation, and only EOS is
            added.
        during_foward: bool
            If True, this is called in the forward method, and both
            EOS and SOS are added.
        """
        batch_dirs = compute_directions(batch_streamlines)

        if self.token_type == 'as_label':
            batch_dirs = add_label_as_last_dim(batch_dirs,
                                               add_sos=True, add_eos=False)
        else:
            batch_dirs = convert_dirs_to_class(
                batch_dirs, self.token_sphere,
                add_sos=True, add_eos=False, to_one_hot=True)

        return batch_dirs

    def _generate_future_mask(self, sz):
        """DO NOT USE FLOAT, their code had a bug (see issue #92554. Fixed in
        latest GitHub branch. Waiting for release.) Using boolean masks.
        """
        mask = Transformer.generate_square_subsequent_mask(sz, self.device)
        return mask < 0

    def _generate_padding_mask(self, unpadded_lengths, batch_max_len):
        nb_streamlines = len(unpadded_lengths)

        mask_padding = torch.full((nb_streamlines, batch_max_len),
                                  fill_value=False, device=self.device)
        for i in range(nb_streamlines):
            mask_padding[i, unpadded_lengths[i]:] = True

        return mask_padding

    def _prepare_masks(self, unpadded_lengths, use_padding, batch_max_len):
        """
        Prepare masks for the transformer.

        Params
        ------
        unpadded_lengths: list
            Length of each streamline.
        use_padding: bool,
            If false, skip padding (all streamlines must have the same length).
        batch_max_len: int
            Batch's maximum length. It is not useful to pad more than that.
            (During tracking, particularly interesting!). Should be equal or
            smaller to self.max_len.

        Returns
        -------
        mask_future: Tensor
            Shape: [batch_max_len, batch_max_len]
            Masks the inputs that do not exist (useful for data generation, but
            also used during training on padded points because, why not), at
            each position.
            = [[False, True,  True],
               [False, False, True],
               [False, False, False]]
        mask_padding: Tensor
            Shape [nb_streamlines, batch_max_len]. Masks positions that do not
            exist in the sequence.
        """
        mask_future = self._generate_future_mask(batch_max_len)

        if use_padding:
            mask_padding = self._generate_padding_mask(unpadded_lengths,
                                                       batch_max_len)
        else:
            mask_padding = None

        return mask_future, mask_padding

    def forward(self, inputs: List[torch.tensor],
                input_streamlines: List[torch.tensor], return_weights=False,
                average_heads=False):
        """
        Params
        ------
        inputs: list[Tensor]
            One tensor per streamline. Size of each tensor =
            [nb_input_points, nb_features].
        input_streamlines: list[Tensor]
            Streamline coordinates. One tensor per streamline. Size of each
            tensor = [nb_input_points + 1, 3]. Directions will be computed to
            obtain targets of the same lengths. Then, directions are used for
            two things:
            - As input to the decoder. This input is generally the shifted
            sequence, with an SOS token (start of sequence) at the first
            position. In our case, there is no token, but the sequence is
            adequately masked to hide future positions. The last direction is
            not used.
            - As target during training. The whole sequence is used.
        return_weights: bool
            If true, returns the weights of the attention layers.
        average_heads: bool
            If return_weights, you may choose to average the weights from
            different heads together.

        Returns
        -------
        output: Tensor,
            Batch output, formatted differently based on context:
                - During training/visu:
                    [total nb points all streamlines, out size]
                - During tracking: [nb streamlines * 1, out size]
        weights: Tuple
            If return_weights: The weights (depending on the child model)
        """
        if self._context is None:
            raise ValueError("Please set context before usage.")

        # Reminder. In all cases, len(each input) == len(each streamline).
        # Correct interpolation and management of points should be done before.
        assert np.all([len(i) == len(s) for i, s in
                       zip(inputs, input_streamlines)])

        # Remember lengths to unpad outputs later.
        # (except during tracking, we only keep the last output, but still
        # verifying if any length exceeds the max allowed).
        unpad_lengths = np.asarray([len(i) for i in inputs])

        # ----------- Checks
        if np.any(unpad_lengths > self.max_len):
            raise ValueError("Some streamlines were longer than accepted max "
                             "length for sequences ({})".format(self.max_len))

        # ----------- Prepare masks and parameters
        # (Skip padding if all streamlines have the same length)
        use_padding = not np.all(unpad_lengths == unpad_lengths[0])
        batch_max_len = np.max(unpad_lengths)
        if CLEAR_CACHE:
            now = time()
            logging.debug("Transformer: Maximal length in batch is {}"
                          .format(batch_max_len))
            torch.torch.cuda.empty_cache()
            now2 = time()
            logging.debug("Cleared cache in {} secs.".format(now2 - now))
        masks = self._prepare_masks(unpad_lengths, use_padding, batch_max_len)

        # Compute targets (= directions) for the decoder.
        dirs = compute_directions(input_streamlines)
        if self.token_type == 'as_label':
            targets = add_label_as_last_dim(dirs, add_sos=True, add_eos=False)
        else:
            targets = convert_dirs_to_class(
                dirs, self.token_sphere, add_sos=True, add_eos=False,
                to_one_hot=True)
        nb_streamlines = len(targets)

        # Start from copy prev option.
        copy_prev_dir = 0.0
        if self.start_from_copy_prev:
            copy_prev_dir = self.copy_prev_dir(dirs)

        # ----------- Ok. Start processing
        # Note. Tried calling torch.cuda.empty_cache() before.
        # Not really recommended, and does not seem to help much.
        # See many discussions in forums, such as
        # https://discuss.pytorch.org/t/about-torch-cuda-empty-cache/34232/26

        # 1. Embedding + position encoding.
        # Run embedding on padded data. Necessary to make the model
        # adapt for the positional encoding.
        inputs, targets = self.run_embedding(inputs, targets, use_padding,
                                             batch_max_len)
        inputs, targets = self.dropout(inputs), self.dropout(targets)

        # 2. Main transformer
        outputs, weights = self._run_main_layer_forward(
            inputs, targets, masks, return_weights, average_heads)

        # Here, data = one tensor, padded.
        # outputs size = [nb streamlines, max_len, d_model].
        # Unpad now and either
        #   a) combine everything for the direction getter, then unstack and
        #   restack when computing loss.  [Chosen here. See if we can improve]
        #   b) loop on direction getter. Stack when computing loss.
        if self._context == 'tracking':
            # If needs to detach: error? Should be using witch torch.no_grad.
            outputs = outputs
            # No need to actually unpad, we only take the last (unpadded)
            # point, newly created. (-1 for python indexing)
            if use_padding:  # Not all the same length (backward tracking)
                outputs = [outputs[i, unpad_lengths[i] - 1, :]
                           for i in range(nb_streamlines)]
                outputs = torch.vstack(outputs)
            else:  # All the same length (ex, during forward tracking)
                outputs = outputs[:, -1, :]
        else:
            # We take all (unpadded) points.
            outputs = [outputs[i, 0:unpad_lengths[i], :]
                       for i in range(nb_streamlines)]
            outputs = torch.vstack(outputs)

        # 3. Direction getter
        # Outputs will be all streamlines merged.
        # To compute loss = ok. During tracking, we will need to split back.
        outputs = self.direction_getter(outputs)
        if self.start_from_copy_prev:
            outputs = copy_prev_dir + outputs

        if self._context != 'tracking':
            outputs = list(torch.split(outputs, list(unpad_lengths)))

        if return_weights:
            return outputs, weights

        return outputs

    def run_embedding(self, inputs, targets, use_padding, batch_max_len):
        """
        Pad + concatenate.
        Embedding. (Add SOS token to target.)
        Positional encoding.
        """
        # toDo: Test faster:
        #   1) stack (2D), embed, unstack, pad_and_stack (3D)
        #   2) loop on streamline to embed, pad_and_stack
        #   3) pad_and_stack, then embed (but we might embed many zeros that
        #      will be masked in attention anyway)
        # Inputs
        inputs = pad_and_stack_batch(inputs, use_padding, batch_max_len)
        inputs = self.embedding_layer_x(inputs)
        inputs = self.position_encoding_layer(inputs)

        # Targets
        targets = pad_and_stack_batch(targets, use_padding, batch_max_len)
        targets = self.embedding_layer_t(targets)
        targets = self.position_encoding_layer(targets)

        return inputs, targets

    def copy_prev_dir(self, dirs):
        if 'regression' in self.dg_key:
            # Regression: The latest previous dir will be used as skip
            # connection on the output.
            # Either take dirs and add [0, 0, 0] at each first position.
            # Or use pre-computed:
            copy_prev_dirs = dirs
        elif self.dg_key == 'sphere-classification':
            # Converting the input directions into classes the same way as
            # during loss, but convert to one-hot.
            # The first previous dir (0) converts to index 0.

            # Not necessarily the same class as previous dirs used as input to
            # the decoder.
            copy_prev_dirs = convert_dirs_to_class(
                dirs, self.direction_getter.torch_sphere, smooth_labels=False,
                add_sos=False, add_eos=False, to_one_hot=True)

        elif self.dg_key == 'smooth-sphere-classification':
            raise NotImplementedError
        elif 'gaussian' in self.dg_key:
            # The mean of the gaussian = the previous dir
            raise NotImplementedError
        else:
            # Fisher: not sure how to do that.
            raise NotImplementedError

        # Add zeros as previous dir at the first position
        copy_prev_dirs = [torch.nn.functional.pad(cp, [0, 0, 1, 0])
                          for cp in copy_prev_dirs]
        copy_prev_dirs = torch.vstack(copy_prev_dirs)

        # Making the one from one-hot important for the sigmoid.
        copy_prev_dirs = copy_prev_dirs * 6.0

        return copy_prev_dirs

    def _run_main_layer_forward(
            self, embed_x: torch.Tensor, embed_t: torch.Tensor, masks: Tuple,
            return_weights: bool, average_heads: bool):
        raise NotImplementedError


class OriginalTransformerModel(AbstractTransformerModel):
    """
    We can use torch.nn.Transformer.
    We will also compare with
    https://github.com/jason9693/MusicTransformer-pytorch.git

                                                 direction getter
                                                        |
                                                     DECODER
                                                  --------------
                                                  |    Norm    |
                                                  |    Skip    |
                                                  |  Dropout   |
                                                  |2-layer FFNN|
                  ENCODER                         |      |     |
               --------------                     |    Norm    |
               |    Norm    | ---------           |    Skip    |
               |    Skip    |         |           |  Dropout   |
               |  Dropout   |         --------->  | Attention  |
               |2-layer FFNN|                     |      |     |
               |     |      |                     |   Norm     |
               |    Norm    |                     |   Skip     |
               |    Skip    |                     |  Dropout   |
               |  Dropout   |                     | Masked Att.|
               | Attention  |                     --------------
               --------------                            |
                     |                             emb_choice_y
                emb_choice_x

    """
    def __init__(self, d_model: int, n_layers_e: int, n_layers_d: int, **kw):
        """
        Args
        ----
        n_layers_e: int
            Number of encoding layers in the encoder. [6]
        n_layers_d: int
            Number of encoding layers in the decoder. [6]
        """
        super().__init__(d_model=d_model, embedding_size_x=d_model,
                         embedding_size_t=d_model, **kw)

        # ----------- Additional params
        self.n_layers_e = n_layers_e
        self.n_layers_d = n_layers_d

        # ----------- Additional instantiations
        logger.info("Instantiating torch transformer, may take a few "
                    "seconds...")
        # Encoder:
        encoder_layer = ModifiedTransformerEncoderLayer(
            self.d_model, self.nheads,
            dim_feedforward=self.ffnn_hidden_size, dropout=self.dropout_rate,
            activation=self.activation, batch_first=True,
            norm_first=self.norm_first)
        encoder = ModifiedTransformerEncoder(encoder_layer, n_layers_e, norm=None)

        # Decoder
        decoder_layer = ModifiedTransformerDecoderLayer(
            self.d_model, self.nheads,
            dim_feedforward=self.ffnn_hidden_size, dropout=self.dropout_rate,
            activation=self.activation, batch_first=True,
            norm_first=self.norm_first)
        decoder = ModifiedTransformerDecoder(decoder_layer, n_layers_d, norm=None)

        self.modified_torch_transformer = ModifiedTransformer(
            self.d_model, self.nheads, n_layers_e, n_layers_d,
            self.ffnn_hidden_size, self.dropout_rate, self.activation,
            encoder, decoder, batch_first=True,
            norm_first=self.norm_first)

    @property
    def params_for_checkpoint(self):
        p = super().params_for_checkpoint
        p.update({
            'n_layers_e': self.n_layers_e,
            'n_layers_d': self.n_layers_d,
            'd_model': self.d_model,
        })
        return p

    def _run_main_layer_forward(self, embed_x, embed_t, masks,
                                return_weights, average_heads):
        """Original Main transformer

        Returns
        -------
        outputs: Tensor
            Shape: [nb_streamlines, max_batch_len, d_model]
        masks: Tuple
            Encoder's self-attention weights: [nb_streamlines, max_batch_len]
        """
        # mask_future, mask_padding = masks
        outputs, sa_weights_encoder, sa_weights_decoder, mha_weights = \
            self.modified_torch_transformer(
                src=embed_x, tgt=embed_t,
                src_mask=masks[0], tgt_mask=masks[0], memory_mask=masks[0],
                src_key_padding_mask=masks[1], tgt_key_padding_mask=masks[1],
                memory_key_padding_mask=masks[1],
                return_weights=return_weights, average_heads=average_heads)
        return outputs, (sa_weights_encoder, sa_weights_decoder, mha_weights)


class TransformerSrcAndTgtModel(AbstractTransformerModel):
    """
    Decoder only. Concatenate source + target together as input.
    See https://arxiv.org/abs/1905.06596 and
    https://proceedings.neurips.cc/paper/2018/file/4fb8a7a22a82c80f2c26fe6c1e0dcbb3-Paper.pdf
    + discussion with Hugo.

                                                        direction getter
                                                              |
                                                  -------| take 1/2 |
                                                  |    Norm      x2 |
                                                  |    Skip      x2 |
                                                  |  Dropout     x2 |
                                                  |2-layer FFNN  x2 |
                                                  |        |        |
                                                  |   Norm       x2 |
                                                  |   Skip       x2 |
                                                  |  Dropout     x2 |
                                                  | Masked Att.  x2 |
                                                  -------------------
                                                           |
                                             [ emb_choice_x ; emb_choice_y ]

    """
    def __init__(self, n_layers_d: int,
                 embedding_size_x: int, embedding_size_t: int, **kw):
        """
        Args
        ----
        n_layers_d: int
            Number of encoding layers in the decoder. [6]
        embedding_size_x: int
            Embedding size for the input. Embedding size for the target will
            be d_model - input.
        """
        super().__init__(d_model=embedding_size_x + embedding_size_t,
                         embedding_size_x=embedding_size_x,
                         embedding_size_t=embedding_size_t, **kw)

        # ----------- Additional params
        self.n_layers_d = n_layers_d

        # ----------- Additional instantiations
        # We say "decoder only" from the logical point of view, but code-wise
        # it is actually "encoder only". A decoder would need output from the
        # encoder.
        logger.debug("Instantiating Transformer...")
        # The d_model is the same; points are not concatenated together.
        # It is the max_len that is modified: The sequences are concatenated
        # one beside the other.
        main_layer_encoder = ModifiedTransformerEncoderLayer(
            self.d_model, self.nheads, dim_feedforward=self.ffnn_hidden_size,
            dropout=self.dropout_rate, activation=self.activation,
            batch_first=True, norm_first=self.norm_first)
        self.modified_torch_transformer = ModifiedTransformerEncoder(
            main_layer_encoder, n_layers_d, norm=None)

    @property
    def params_for_checkpoint(self):
        p = super().params_for_checkpoint
        p.update({
            'n_layers_d': self.n_layers_d,
            'embedding_size_x': self.embedding_size_x,
            'embedding_size_t': self.embedding_size_t
        })
        return p

    def run_embedding(self, inputs: List[torch.Tensor], targets, use_padding,
                      batch_max_len):
        """
        Pad + concatenate.
        Embedding. (Add SOS token to target.)
        Positional encoding.
        """
        # Compared to super: possibly skip the positional encoding on the
        # target direction. Could help learn to copy previous direction.

        # Inputs
        inputs = pad_and_stack_batch(inputs, use_padding, batch_max_len)
        inputs = self.embedding_layer_x(inputs)
        inputs = self.position_encoding_layer(inputs)

        # Targets
        targets = pad_and_stack_batch(targets, use_padding, batch_max_len)
        targets = self.embedding_layer_t(targets)
        # targets = self.position_encoding_layer(targets)

        return inputs, targets

    def _run_main_layer_forward(self, embed_x, embed_t, masks,
                                return_weights, average_heads):
        # mask_future, mask_padding = masks

        # Concatenating x and t on the last dimension.
        inputs = torch.cat((embed_x, embed_t), dim=-1)

        # Main transformer
        outputs, sa_weights = self.modified_torch_transformer(
            src=inputs, mask=masks[0], src_key_padding_mask=masks[1],
            return_weights=return_weights, average_heads=average_heads)

        return outputs, (sa_weights,)
