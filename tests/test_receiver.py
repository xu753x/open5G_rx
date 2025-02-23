import numpy as np
import scipy
import os
import pytest
import logging
import matplotlib.pyplot as plt
import importlib.util

import cocotb
import cocotb_test.simulator
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import py3gpp
import sigmf

CLK_PERIOD_NS = 8
CLK_PERIOD_S = CLK_PERIOD_NS * 0.000000001
tests_dir = os.path.abspath(os.path.dirname(__file__))
rtl_dir = os.path.abspath(os.path.join(tests_dir, '..', 'hdl'))

def _twos_comp(val, bits):
    """compute the 2's complement of int value val"""
    if (val & (1 << (bits - 1))) != 0:
        val = val - (1 << bits)
    return int(val)

class TB(object):
    def __init__(self, dut):
        self.dut = dut
        self.IN_DW = int(dut.IN_DW.value)
        self.OUT_DW = int(dut.OUT_DW.value)
        self.TAP_DW = int(dut.TAP_DW.value)
        self.PSS_LEN = int(dut.PSS_LEN.value)
        self.WINDOW_LEN = int(dut.WINDOW_LEN.value)
        self.HALF_CP_ADVANCE = int(dut.HALF_CP_ADVANCE.value)
        self.LLR_DW = int(dut.LLR_DW.value)
        self.NFFT = int(dut.NFFT.value)
        self.MULT_REUSE = int(dut.MULT_REUSE.value)

        self.log = logging.getLogger('cocotb.tb')
        self.log.setLevel(logging.DEBUG)

        cocotb.start_soon(Clock(self.dut.clk_i, CLK_PERIOD_NS, units='ns').start())
        cocotb.start_soon(Clock(self.dut.sample_clk_i, CLK_PERIOD_NS, units='ns').start())  # TODO make sample_clk_i 3.84 MHz and clk_i 100 MHz

    async def cycle_reset(self):
        self.dut.s_axis_in_tvalid.value = 0
        self.dut.reset_n.value = 1
        await RisingEdge(self.dut.clk_i)
        self.dut.reset_n.value = 0
        await RisingEdge(self.dut.clk_i)
        self.dut.reset_n.value = 1
        await RisingEdge(self.dut.clk_i)

    async def read_axil(self, addr):
        self.dut.s_axi_if_araddr.value = addr
        self.dut.s_axi_if_arvalid.value = 1
        self.dut.s_axi_if_rready.value = 1
        await RisingEdge(self.dut.clk_i)
        while self.dut.s_axi_if_arready.value == 0:
            await RisingEdge(self.dut.clk_i)
        while self.dut.s_axi_if_rvalid.value == 0:
            await RisingEdge(self.dut.clk_i)
        self.dut.s_axi_if_arvalid.value = 0
        self.dut.s_axi_if_rready.value = 0
        data = self.dut.s_axi_if_rdata.value.integer
        return data


@cocotb.test()
async def simple_test(dut):
    tb = TB(dut)
    FILE = '../../tests/' + os.environ['TEST_FILE'] + '.sigmf-data'
    handle = sigmf.sigmffile.fromfile(FILE)
    waveform = handle.read_samples()
    fs = handle.get_global_field(sigmf.SigMFFile.SAMPLE_RATE_KEY)
    NFFT = tb.NFFT
    FFT_LEN = 2 ** NFFT
    dec_factor = int((2048 * fs // 30720000) // (2 ** tb.NFFT))
    assert dec_factor != 0, f'NFFT = {tb.NFFT} and fs = {fs} is not possible!'
    print(f'test_file = {FILE} with {len(waveform)} samples')
    print(f'sample_rate = {fs}, decimation_factor = {dec_factor}')
    if dec_factor > 1:
        fs_dec = fs // dec_factor
        waveform = scipy.signal.decimate(waveform, dec_factor, ftype='fir')
    else:
        fs_dec = fs

    RND_JITTER = int(os.getenv('RND_JITTER'))
    PSS_IDLE_CLKS = int(fs_dec // 1920000)
    print(f'FREE_CYCLES = {PSS_IDLE_CLKS}')
    EXTRA_IDLE_CLKS = 0 if PSS_IDLE_CLKS >= tb.MULT_REUSE else tb.MULT_REUSE // PSS_IDLE_CLKS - 1 # insert additional valid 0 cycles if needed
    print(f'additional idle cycles per sample: {EXTRA_IDLE_CLKS}')
    MAX_AMPLITUDE = (2 ** (tb.IN_DW // 2 - 1) - 1)
    if os.environ['TEST_FILE'] == '30720KSPS_dl_signal':
        expect_exact_timing = False
        expected_N_id_1 = 69
        expected_N_id_2 = 2
        expected_ibar_SSB = 0
        N_SSBs = 4
        MAX_TX = int((0.005 + 0.02 * (N_SSBs - 1)) * fs_dec)
        MAX_CLK_CNT = int(MAX_TX * (1 + EXTRA_IDLE_CLKS + RND_JITTER * 0.5) + 10000)
        waveform /= max(np.abs(waveform.real.max()), np.abs(waveform.imag.max()))
        waveform *= MAX_AMPLITUDE * 0.8  # need this 0.8 because rounding errors caused overflows, nasty bug!
    elif os.environ['TEST_FILE'] == '772850KHz_3840KSPS_low_gain':
        # waveform = waveform[int(0.04 * fs_dec):]
        expect_exact_timing = False
        expected_N_id_1 = 0x123
        expected_N_id_2 = 0
        expected_ibar_SSB = 3
        N_SSBs = 4
        MAX_TX = int((0.01 + 0.02 * (N_SSBs - 1)) * fs_dec)
        MAX_CLK_CNT = int(MAX_TX * (1 + EXTRA_IDLE_CLKS + RND_JITTER * 0.5) + 10000)
        delta_f = -4e3
        waveform = waveform * np.exp(-1j*(2*np.pi*delta_f/fs_dec*np.arange(waveform.shape[0])))
        waveform *= 2**19
    elif os.environ['TEST_FILE'] == '762000KHz_3840KSPS_low_gain':
        expect_exact_timing = False
        expected_N_id_1 = 103
        expected_N_id_2 = 0
        expected_ibar_SSB = 3
        N_SSBs = 4
        MAX_TX = int((0.01 + 0.02 * (N_SSBs - 1)) * fs_dec)
        MAX_CLK_CNT = int(MAX_TX * (1 + EXTRA_IDLE_CLKS + RND_JITTER * 0.5) + 10000)
        delta_f = 0e3
        waveform = waveform * np.exp(-1j*(2*np.pi*delta_f/fs_dec*np.arange(waveform.shape[0])))
        waveform *= 2**19
    elif os.environ['TEST_FILE'] == '763450KHz_7680KSPS_low_gain':
        expect_exact_timing = False
        expected_N_id_1 = 103
        expected_N_id_2 = 0
        expected_ibar_SSB = 3
        N_SSBs = 4
        MAX_TX = int((0.01 + 0.02 * (N_SSBs - 1)) * fs_dec)
        MAX_CLK_CNT = int(MAX_TX * (1 + EXTRA_IDLE_CLKS + RND_JITTER * 0.5) + 10000)
        delta_f = 0e3
        waveform = waveform * np.exp(-1j*(2*np.pi*delta_f/fs_dec*np.arange(waveform.shape[0])))
        waveform *= 2**19
    else:
        file_string = os.environ['TEST_FILE']
        assert False, f'test file {file_string} is not supported'
    expected_N_id = expected_N_id_1 * 3 + expected_N_id_2

    CFO = int(os.getenv('CFO'))
    print(f'CFO = {CFO} Hz')
    waveform *= np.exp(np.arange(len(waveform)) * 1j * 2 * np.pi * CFO / fs_dec)

    assert np.abs(waveform.real).max().astype(int) <= MAX_AMPLITUDE, 'Error: input data overflow!'
    assert np.abs(waveform.imag).max().astype(int) <= MAX_AMPLITUDE, 'Error: input data overflow!'
    waveform = waveform.real.astype(int) + 1j * waveform.imag.astype(int)

    await tb.cycle_reset()
    USE_COCOTB_AXI = 0

    if USE_COCOTB_AXI:
        # cocotbext-axi hangs with Verilator -> https://github.com/verilator/verilator/issues/3919
        # case_insensitive=False is a workaround https://github.com/alexforencich/verilog-axi/issues/48
        axi_master = AxiLiteMaster(AxiLiteBus.from_prefix(dut, 's_axi_if', case_insensitive=False), dut.clk_i, dut.reset_n, reset_active_level = False)
        addr = 0
        data = await axi_master.read_dword(4 * addr)
        data = int(data)
        assert data == 0x00010069
        addr = 5
        data = await axi_master.read_dword(4 * addr)
        data = int(data)
        assert data == 0x00000000

        OFFSET_ADDR_WIDTH = 16 - 2
        addr = 0
        data = await axi_master.read_dword(addr + (1 << OFFSET_ADDR_WIDTH))
        data = int(data)
        assert data == 0x00010061

    else:
        data = await tb.read_axil(0)
        print(f'axi-lite fifo: id = {data:x}')
        assert data == 0x00010069
        data = await tb.read_axil(5 * 4)
        print(f'axi-lite fifo: level = {data}')

        OFFSET_ADDR_WIDTH = 16 - 2
        data = await tb.read_axil(0 + (1 << OFFSET_ADDR_WIDTH))
        print(f'PSS detector: id = {data:x}')
        assert data == 0x00010061

    clk_cnt = 0
    received = []
    rx_ADC_data = []
    received_PBCH = []
    received_SSS = []
    corrected_PBCH = []
    received_PBCH_LLR = []
    received_N_ids = []
    received_ibar_SSB = []
    if NFFT == 8:
        N_PRB = 20
    elif NFFT == 9:
        N_PRB = 25
    SYMBOLS_PER_PRB = 12
    FFT_OUT_DW = 16
    SYMBOL_LEN = N_PRB * SYMBOLS_PER_PRB
    N_PRB_PBCH = 20
    PBCH_SYMBOL_LEN = N_PRB_PBCH * SYMBOLS_PER_PRB
    NUM_TIMESTAMP_SAMPLES = 64 // FFT_OUT_DW
    RGS_TRANSFER_LEN = SYMBOL_LEN + NUM_TIMESTAMP_SAMPLES + 1
    print(RGS_TRANSFER_LEN)
    received_rgs = np.zeros((1, RGS_TRANSFER_LEN), 'complex')
    num_rgs_symbols = 0
    rgs_sc_idx = 0
    HALF_CP_ADVANCE = tb.HALF_CP_ADVANCE
    CP2_LEN = 18 * FFT_LEN // 256
    SSS_LEN = 127
    SSS_START = FFT_LEN // 2 - (SSS_LEN + 1) // 2
    clk_div = 0
    tx_cnt = 0
    sample_cnt = 0
    random_extra_cycle = 0
    random_seq = (py3gpp.nrPSS(0)[:-1] + 1) // 2 # only use 126 bits to get an equal number of 0s and 1s
    while clk_cnt < MAX_CLK_CNT:
        await RisingEdge(dut.clk_i)
        if (tx_cnt < MAX_TX) and (clk_div == 0 or EXTRA_IDLE_CLKS == 0):
            clk_div += 1
            data = (((int(waveform[tx_cnt].imag)  & ((2 ** (tb.IN_DW // 2)) - 1)) << (tb.IN_DW // 2)) \
                + ((int(waveform[tx_cnt].real)) & ((2 ** (tb.IN_DW // 2)) - 1))) & ((2 ** tb.IN_DW) - 1)
            tx_cnt += 1
            dut.s_axis_in_tdata.value = data
            dut.s_axis_in_tvalid.value = 1
        else:
            dut.s_axis_in_tvalid.value = 0
            if clk_div == EXTRA_IDLE_CLKS + random_extra_cycle:
                clk_div = 0
                if RND_JITTER:
                    random_extra_cycle = random_seq[clk_cnt % 126]
            else:
                clk_div += 1

        clk_cnt += 1

        if dut.ibar_SSB_valid_o.value.integer:
            received_ibar_SSB.append(dut.ibar_SSB_o.value.integer)

        if dut.peak_detected_debug_o.value.integer:
            received.append(sample_cnt)
            print(f'peak pos = {sample_cnt}')
        sample_cnt += dut.m_axis_PSS_out_tvalid.value.integer

        if dut.N_id_valid_o.value.integer:
            print(f'detected N_id = {dut.N_id_o.value.integer}')
            received_N_ids.append(dut.N_id_o.value.integer)

        if dut.m_axis_SSS_tvalid.value.integer:
            print(f'detected N_id_1 = {dut.m_axis_SSS_tdata.value.integer}')

        if dut.m_axis_llr_out_tvalid.value == 1 and dut.m_axis_llr_out_tuser.value == 1:
            received_PBCH_LLR.append(_twos_comp(dut.m_axis_llr_out_tdata.value.integer & (2 ** (tb.LLR_DW) - 1), tb.LLR_DW))

        if dut.m_axis_cest_out_tvalid.value == 1 and ((dut.m_axis_cest_out_tuser.value & 0x03) == 1):
            blk_exp = dut.m_axis_cest_out_tuser.value.integer >> 2
            corrected_PBCH.append((_twos_comp(dut.m_axis_cest_out_tdata.value.integer & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2)
                + 1j * _twos_comp((dut.m_axis_cest_out_tdata.value.integer >> (FFT_OUT_DW//2)) & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2)) / (2 ** blk_exp))

        if dut.PBCH_valid_o.value.integer == 1:
            # print(f"rx PBCH[{len(received_PBCH):3d}] re = {dut.m_axis_out_tdata.value.integer & (2**(FFT_OUT_DW//2) - 1):4x} " \
            #     "im = {(dut.m_axis_out_tdata.value.integer>>(FFT_OUT_DW//2)) & (2**(FFT_OUT_DW//2) - 1):4x}")
            received_PBCH.append(_twos_comp(dut.m_axis_demod_out_tdata.value.integer & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2)
                + 1j * _twos_comp((dut.m_axis_demod_out_tdata.value.integer >> (FFT_OUT_DW//2)) & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2))

        if dut.SSS_valid_o.value.integer == 1:
            received_SSS.append(_twos_comp(dut.m_axis_demod_out_tdata.value.integer & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2)
                + 1j * _twos_comp((dut.m_axis_demod_out_tdata.value.integer >> (FFT_OUT_DW//2)) & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2))

        if dut.m_axis_out_tvalid.value.integer:
            if rgs_sc_idx < 1 + NUM_TIMESTAMP_SAMPLES:
                received_rgs[num_rgs_symbols, rgs_sc_idx] = dut.m_axis_out_tdata.value.integer
            else:
                received_rgs[num_rgs_symbols, rgs_sc_idx] = _twos_comp(dut.m_axis_out_tdata.value.integer & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2) \
                    + 1j * _twos_comp((dut.m_axis_out_tdata.value.integer >> (FFT_OUT_DW//2)) & (2**(FFT_OUT_DW//2) - 1), FFT_OUT_DW//2)

            if dut.m_axis_out_tlast.value.integer:
                num_rgs_symbols += 1
                assert rgs_sc_idx == RGS_TRANSFER_LEN - 1, print('Error: wrong received number of bytes from ressource_grid_subscriber!')
                rgs_sc_idx = 0
                received_rgs = np.vstack((received_rgs, np.zeros((1, RGS_TRANSFER_LEN), 'complex')))
            else:
                rgs_sc_idx += 1

    print(f'received {len(corrected_PBCH)} PBCH IQ samples')
    print(f'received {len(received_PBCH_LLR)} PBCH LLRs samples')
    assert len(received_SSS) == N_SSBs * SSS_LEN, print(f'expected {N_SSBs * SSS_LEN} SSS carrier but received {len(received_SSS)}')
    assert len(corrected_PBCH) == 432 * (N_SSBs - 1), print('received PBCH does not have correct length!')
    assert len(received_PBCH_LLR) == 432 * 2 * (N_SSBs - 1), print('received PBCH LLRs do not have correct length!')
    assert not np.array_equal(np.array(received_PBCH_LLR), np.zeros(len(received_PBCH_LLR)))
    assert received_ibar_SSB[0] == expected_ibar_SSB, print('wrong ibar_SSB detected!')

    fifo_data = []
    if USE_COCOTB_AXI:
        addr = 5
        data = await axi_master.read_dword(4 * addr)
        data = int(data)
        assert data >= 864 * 2
        for i in range(864 * 2):
            data = await axi_master.read_dword(7 * 4)
            fifo_data.append(_twos_comp(data & (2 ** (tb.LLR_DW) - 1), tb.LLR_DW))
    else:
        addr = 0
        data = await tb.read_axil(addr * 4)
        print(f'axi-lite fifo: id = {data:x}')
        addr = 5
        data = await tb.read_axil(addr * 4)
        print(f'axi-lite fifo: level = {data}')
        assert data >= 864 * 2
        addr = 7
        for i in range(864 * 2):
            data = await tb.read_axil(addr * 4)
            fifo_data.append(_twos_comp(data & (2 ** (tb.LLR_DW) - 1), tb.LLR_DW))
    assert not np.array_equal(np.array(fifo_data), np.zeros(len(fifo_data)))
    assert np.array_equal(np.array(received_PBCH_LLR)[:864 * 2], np.array(fifo_data))

    rx_ADC_data = waveform[received[0] + CP2_LEN + FFT_LEN - 1:][:MAX_TX]
    CP_ADVANCE = CP2_LEN // 2 if HALF_CP_ADVANCE else CP2_LEN
    ideal_SSS_sym = np.fft.fftshift(np.fft.fft(rx_ADC_data[CP2_LEN + FFT_LEN + CP_ADVANCE:][:FFT_LEN]))
    ideal_SSS_sym *= np.exp(1j * ( 2 * np.pi * (CP2_LEN - CP_ADVANCE) / FFT_LEN * np.arange(FFT_LEN) + np.pi * (CP2_LEN - CP_ADVANCE)))
    ideal_SSS = ideal_SSS_sym[SSS_START:][:SSS_LEN]
    if 'PLOTS' in os.environ and os.environ['PLOTS'] == '1':
        ax = plt.subplot(2, 4, 1)
        ax.set_title('model whole symbol')
        ax.plot(np.abs(ideal_SSS_sym))
        ax = plt.subplot(2, 4, 2)
        ax.set_title('model used SCs abs')
        ax.plot(np.abs(ideal_SSS), 'r-')
        ax = plt.subplot(2, 4, 3)
        ax.set_title('model used SCs I/Q')
        ax.plot(np.real(ideal_SSS), 'r-')
        ax = ax.twinx()
        ax.plot(np.imag(ideal_SSS), 'b-')
        ax = plt.subplot(2, 4, 4)
        ax.set_title('model used SCs constellation')
        ax.plot(np.real(ideal_SSS), np.imag(ideal_SSS), 'r.')

        ax = plt.subplot(2, 4, 6)
        ax.set_title('hdl used SCs abs')
        ax.plot(np.abs(received_SSS[:SSS_LEN]), 'r-')
        ax.plot(np.abs(received_SSS[SSS_LEN:][:SSS_LEN]), 'b-')
        ax = plt.subplot(2, 4, 7)
        ax.set_title('hdl used SCs I/Q')
        ax.plot(np.real(received_SSS[:SSS_LEN]), 'r-')
        ax = ax.twinx()
        ax.plot(np.imag(received_SSS[:SSS_LEN]), 'b-')
        ax = plt.subplot(2, 4, 8)
        ax.set_title('hdl used SCs I/Q constellation')
        ax.plot(np.real(received_SSS[:SSS_LEN]), np.imag(received_SSS[:SSS_LEN]), 'r.')
        ax.plot(np.real(received_SSS[SSS_LEN:][:SSS_LEN]), np.imag(received_SSS[:SSS_LEN]), 'b.')
        plt.show()

    received_PBCH_ideal = np.fft.fftshift(np.fft.fft(rx_ADC_data[CP_ADVANCE:][:FFT_LEN]))
    received_PBCH_ideal *= np.exp(1j * ( 2 * np.pi * (CP2_LEN - CP_ADVANCE) / FFT_LEN * np.arange(FFT_LEN) + np.pi * (CP2_LEN - CP_ADVANCE)))
    received_PBCH_ideal = received_PBCH_ideal[8:][:PBCH_SYMBOL_LEN]
    received_PBCH_ideal = (received_PBCH_ideal.real.astype(int) + 1j * received_PBCH_ideal.imag.astype(int))
    if 'PLOTS' in os.environ and os.environ['PLOTS'] == '1':
        _, axs = plt.subplots(1, 3, figsize=(10, 5))
        axs[0].set_title('CFO corrected SSS')
        axs[0].plot(np.real(received_SSS)[:SSS_LEN], np.imag(received_SSS)[:SSS_LEN], 'r.')
        axs[0].plot(np.real(received_SSS)[SSS_LEN:][:SSS_LEN], np.imag(received_SSS)[SSS_LEN:][:SSS_LEN], 'b.')

        axs[1].set_title('CFO corrected PBCH')
        axs[1].plot(np.real(received_PBCH[:PBCH_SYMBOL_LEN]), np.imag(received_PBCH[:PBCH_SYMBOL_LEN]), 'r.')
        axs[1].plot(np.real(received_PBCH[3*PBCH_SYMBOL_LEN:][:PBCH_SYMBOL_LEN]), np.imag(received_PBCH[3*PBCH_SYMBOL_LEN:][:PBCH_SYMBOL_LEN]), 'g.')
        axs[1].plot(np.real(received_PBCH[6*PBCH_SYMBOL_LEN:][:PBCH_SYMBOL_LEN]), np.imag(received_PBCH[6*PBCH_SYMBOL_LEN:][:PBCH_SYMBOL_LEN]), 'b.')
        #axs[2].plot(np.real(received_PBCH_ideal), np.imag(received_PBCH_ideal), 'y.')

        axs[2].set_title('CFO and channel corrected PBCH')
        axs[2].plot(np.real(corrected_PBCH[:180]), np.imag(corrected_PBCH[:180]), 'r.')
        axs[2].plot(np.real(corrected_PBCH[180:][:72]), np.imag(corrected_PBCH[180:][:72]), 'g.')
        axs[2].plot(np.real(corrected_PBCH[180 + 72:][:180]), np.imag(corrected_PBCH[180 + 72:][:180]), 'b.')
        plt.show()

    print(f'first peak at {received[0]}')

    scaling_factor = 2 ** (tb.IN_DW + NFFT - tb.OUT_DW) # FFT core is in truncation mode
    ideal_SSS = ideal_SSS.real / scaling_factor + 1j * ideal_SSS.imag / scaling_factor

    # verify PSS_detector
    if os.environ['TEST_FILE'] == '30720KSPS_dl_signal':
        if NFFT == 8:
            assert received[0] == 551
        elif NFFT == 9:
            assert received[0] == 1101
        else:
            assert False
    elif os.environ['TEST_FILE'] == '772850KHz_3840KSPS_low_gain':
        if NFFT == 8:
            assert received[0] == 2113
        else:
            assert False
    elif os.environ['TEST_FILE'] == '763450KHz_7680KSPS_low_gain':
        if NFFT == 9:
            assert received[0] == 56773
        else:
            assert False
    else:
        assert False

    # verify detected N_ids
    for N_id in received_N_ids:
        assert N_id == expected_N_id, print(f'wrong N_id: expected {expected_N_id} but received {N_id}')

    # verify received SSS sequence
    corr = np.zeros(335)
    for i in range(335):
        sss = py3gpp.nrSSS(i * 3 + expected_N_id_2)
        corr[i] = np.abs(np.vdot(sss, received_SSS[:SSS_LEN]))
    detected_N_id_1 = np.argmax(corr)
    assert detected_N_id_1 == expected_N_id_1
    detected_N_id = detected_N_id_1 * 3 + expected_N_id_2

    # verify received ressource_grid_subscriber
    def extract_timestamp(packet):
        ts = 0
        samples = packet[1:][:NUM_TIMESTAMP_SAMPLES]
        for i in range(NUM_TIMESTAMP_SAMPLES):
            ts += int(samples[i].real) << int(FFT_OUT_DW * i)
        return ts

    timestamp = extract_timestamp(received_rgs[0, :])
    for i in range(1, num_rgs_symbols):
        delta_samples = extract_timestamp(received_rgs[i, :]) - timestamp
        # print(f'delta_samples = {delta_samples}')
        corr_factor = 2 ** (NFFT - 8)
        if expect_exact_timing:
            assert delta_samples in [274 * corr_factor, 276 * corr_factor], print('Error: symbol timestamps don\'t align!') # depending on cp1 or cp2
        else:
            if delta_samples not in [274 * corr_factor, 276 * corr_factor]:
                print(f'timing deviation: delta_samples = {delta_samples}')
        timestamp = extract_timestamp(received_rgs[i, :])

    # verify channel_estimator and demap
    # try to decode PBCH
    ibar_SSB = expected_ibar_SSB
    nVar = 1
    corrected_PBCH = np.array(corrected_PBCH)[:432]
    for mode in ['hard', 'soft', 'hdl']:
        print(f'demodulation mode: {mode}')
        if mode == 'hdl':
            pbchBits = np.array(fifo_data)[:432 * 2]
        else:
            pbchBits = py3gpp.nrSymbolDemodulate(corrected_PBCH, 'QPSK', nVar, mode)

        E = 864
        v = ibar_SSB
        scrambling_seq = py3gpp.nrPBCHPRBS(detected_N_id, v, E)
        scrambling_seq_bpsk = (-1) * scrambling_seq * 2 + 1
        pbchBits_descrambled = pbchBits * scrambling_seq_bpsk

        A = 32
        P = 24
        K = A+P
        N = 512 # calculated according to Section 5.3.1 of 3GPP TS 38.212
        decIn = py3gpp.nrRateRecoverPolar(pbchBits_descrambled, K, N, False, discardRepetition=False)
        decoded = py3gpp.nrPolarDecode(decIn, K, 0, 0)

        # check CRC
        print(decoded)
        _, crc_result = py3gpp.nrCRCDecode(decoded, '24C')
        if crc_result == 0:
            print('nrPolarDecode: PBCH CRC ok')
        else:
            print('nrPolarDecode: PBCH CRC failed')
        assert crc_result == 0

@pytest.mark.parametrize('IN_DW', [32])
@pytest.mark.parametrize('OUT_DW', [32])
@pytest.mark.parametrize('TAP_DW', [32])
@pytest.mark.parametrize('WINDOW_LEN', [8])
@pytest.mark.parametrize('CFO', [0, 1200])
@pytest.mark.parametrize('HALF_CP_ADVANCE', [0, 1])
@pytest.mark.parametrize('USE_TAP_FILE', [1])
@pytest.mark.parametrize('LLR_DW', [8])
@pytest.mark.parametrize('NFFT', [8])
@pytest.mark.parametrize('MULT_REUSE', [0, 2, 4])
@pytest.mark.parametrize('INITIAL_DETECTION_SHIFT', [4])
@pytest.mark.parametrize('INITIAL_CFO_MODE', [0])
@pytest.mark.parametrize('RND_JITTER', [0])
def test(IN_DW, OUT_DW, TAP_DW, WINDOW_LEN, CFO, HALF_CP_ADVANCE, USE_TAP_FILE, LLR_DW, NFFT, MULT_REUSE,
         INITIAL_DETECTION_SHIFT, INITIAL_CFO_MODE, RND_JITTER, FILE = '30720KSPS_dl_signal', HAS_CFO_COR = 0):
    dut = 'receiver'
    module = os.path.splitext(os.path.basename(__file__))[0]
    toplevel = dut

    unisim_dir = os.path.join(rtl_dir, '../submodules/FFT/submodules/XilinxUnisimLibrary/verilog/src/unisims')
    verilog_sources = [
        os.path.join(rtl_dir, f'{dut}.sv'),
        os.path.join(rtl_dir, 'receiver_regmap.sv'),
        os.path.join(rtl_dir, 'axil_interconnect_wrap_1x4.v'),
        os.path.join(rtl_dir, 'verilog-axi', 'axil_interconnect.v'),
        os.path.join(rtl_dir, 'verilog-axi', 'arbiter.v'),
        os.path.join(rtl_dir, 'verilog-axi', 'priority_encoder.v'),
        os.path.join(rtl_dir, 'atan.sv'),
        os.path.join(rtl_dir, 'atan2.sv'),
        os.path.join(rtl_dir, 'div.sv'),
        os.path.join(rtl_dir, 'AXIS_FIFO.sv'),
        os.path.join(rtl_dir, 'frame_sync.sv'),
        os.path.join(rtl_dir, 'frame_sync_regmap.sv'),
        os.path.join(rtl_dir, 'channel_estimator.sv'),
        os.path.join(rtl_dir, 'axis_fifo_asym.sv'),
        os.path.join(rtl_dir, 'demap.sv'),
        os.path.join(rtl_dir, 'PSS_detector_regmap.sv'),
        os.path.join(rtl_dir, 'AXI_lite_interface.sv'),
        os.path.join(rtl_dir, 'PSS_detector.sv'),
        os.path.join(rtl_dir, 'CFO_calc.sv'),
        os.path.join(rtl_dir, 'Peak_detector.sv'),
        os.path.join(rtl_dir, 'PSS_correlator.sv'),
        os.path.join(rtl_dir, 'PSS_correlator_mr.sv'),
        os.path.join(rtl_dir, 'SSS_detector.sv'),
        os.path.join(rtl_dir, 'LFSR/LFSR.sv'),
        os.path.join(rtl_dir, 'FFT_demod.sv'),
        os.path.join(rtl_dir, 'axis_axil_fifo.sv'),
        os.path.join(rtl_dir, 'DDS', 'dds.sv'),
        os.path.join(rtl_dir, 'complex_multiplier', 'complex_multiplier.sv'),
        os.path.join(rtl_dir, 'CIC/cic_d.sv'),
        os.path.join(rtl_dir, 'CIC/comb.sv'),
        os.path.join(rtl_dir, 'CIC/downsampler.sv'),
        os.path.join(rtl_dir, 'CIC/integrator.sv'),
        os.path.join(rtl_dir, 'FFT/fft/fft.v'),
        os.path.join(rtl_dir, 'FFT/fft/int_dif2_fly.v'),
        os.path.join(rtl_dir, 'FFT/fft/int_fftNk.v'),
        os.path.join(rtl_dir, 'FFT/math/int_addsub_dsp48.v'),
        os.path.join(rtl_dir, 'FFT/math/cmult/int_cmult_dsp48.v'),
        os.path.join(rtl_dir, 'FFT/math/cmult/int_cmult18x25_dsp48.v'),
        os.path.join(rtl_dir, 'FFT/twiddle/rom_twiddle_int.v'),
        os.path.join(rtl_dir, 'FFT/delay/int_align_fft.v'),
        os.path.join(rtl_dir, 'FFT/delay/int_delay_line.v'),
        os.path.join(rtl_dir, 'FFT/buffers/inbuf_half_path.v'),
        os.path.join(rtl_dir, 'FFT/buffers/outbuf_half_path.v'),
        os.path.join(rtl_dir, 'FFT/buffers/int_bitrev_order.v'),
        os.path.join(rtl_dir, 'FFT/buffers/dynamic_block_scaling.v'),
        os.path.join(rtl_dir, 'ressource_grid_framer.sv'),
        os.path.join(rtl_dir, 'BWP_extractor.sv'),
    ]
    if os.environ.get('SIM') != 'verilator':
        verilog_sources.append(os.path.join(rtl_dir, '../submodules/FFT/submodules/XilinxUnisimLibrary/verilog/src/glbl.v'))

    includes = [
        os.path.join(rtl_dir, 'CIC'),
        os.path.join(rtl_dir, 'fft-core')
    ]

    PSS_LEN = 128
    SAMPLE_RATE = 3840000 * (2 ** NFFT) // 256
    CIC_DEC = SAMPLE_RATE // 1920000
    print(f'CIC decimation = {CIC_DEC}')
    MULT_REUSE_FFT = MULT_REUSE // CIC_DEC if MULT_REUSE > 2 else 1 # insert valid = 0 cycles if needed
    CLK_FREQ = str(int(SAMPLE_RATE * MULT_REUSE_FFT))
    print(f'system clock frequency = {CLK_FREQ} Hz')
    print(f'sample clock frequency = {SAMPLE_RATE} Hz')
    parameters = {}
    parameters['IN_DW'] = IN_DW
    parameters['OUT_DW'] = OUT_DW
    parameters['TAP_DW'] = TAP_DW
    parameters['PSS_LEN'] = PSS_LEN
    parameters['WINDOW_LEN'] = WINDOW_LEN
    parameters['HALF_CP_ADVANCE'] = HALF_CP_ADVANCE
    parameters['USE_TAP_FILE'] = USE_TAP_FILE
    parameters['LLR_DW'] = LLR_DW
    parameters['NFFT'] = NFFT
    parameters['MULT_REUSE'] = MULT_REUSE
    parameters['CLK_FREQ'] = CLK_FREQ
    parameters['INITIAL_DETECTION_SHIFT'] = INITIAL_DETECTION_SHIFT
    parameters['INITIAL_CFO_MODE'] = INITIAL_CFO_MODE
    parameters['MULT_REUSE_FFT'] = MULT_REUSE_FFT
    os.environ['CFO'] = str(CFO)
    os.environ['RND_JITTER'] = str(RND_JITTER)
    parameters_dirname = parameters.copy()
    parameters_dirname['CFO'] = CFO
    parameters_dirname['RND_JITTER'] = RND_JITTER
    folder = 'receiver_' + '_'.join(('{}={}'.format(*i) for i in parameters_dirname.items())) + '_' + FILE
    sim_build = os.path.join('sim_build/', folder)
    os.environ['TEST_FILE'] = FILE

    # the following parameters don't appear in the filename
    parameters['HAS_CFO_COR'] = HAS_CFO_COR

    # prepare FFT_demod taps
    FFT_LEN = 2 ** NFFT
    CP_LEN = int(18 * FFT_LEN / 256)  # TODO: only CP2 supported so far! another lut for CP1 symbols is needed or use same CP_ADVANCE for CP1.
    CP_ADVANCE = CP_LEN // 2
    FFT_OUT_DW = 16
    file_path = os.path.abspath(os.path.join(tests_dir, '../tools/generate_FFT_demod_tap_file.py'))
    spec = importlib.util.spec_from_file_location("generate_FFT_demod_tap_file", file_path)
    generate_FFT_demod_tap_file = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate_FFT_demod_tap_file)
    generate_FFT_demod_tap_file.main(['--NFFT', str(NFFT),'--CP_LEN', str(CP_LEN), '--CP_ADVANCE', str(CP_ADVANCE),
                                      '--OUT_DW', str(FFT_OUT_DW), '--path', sim_build])

    # prepare PSS_correlator taps
    for N_id_2 in range(3):
        os.makedirs(sim_build, exist_ok=True)
        file_path = os.path.abspath(os.path.join(tests_dir, '../tools/generate_PSS_tap_file.py'))
        spec = importlib.util.spec_from_file_location("generate_PSS_tap_file", file_path)
        generate_PSS_tap_file = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generate_PSS_tap_file)
        generate_PSS_tap_file.main(['--PSS_LEN', str(PSS_LEN),'--TAP_DW', str(TAP_DW), '--N_id_2', str(N_id_2), '--path', sim_build])

    extra_env = {f'PARAM_{k}': str(v) for k, v in parameters.items()}

    compile_args = []
    if os.environ.get('SIM') == 'verilator':
        compile_args = ['--no-timing', '-Wno-fatal', '-Wno-width', '-Wno-PINMISSING', '-y', tests_dir + '/../submodules/verilator-unisims']
    else:
        compile_args = ['-sglbl', '-y' + unisim_dir]
    cocotb_test.simulator.run(
        python_search=[tests_dir],
        verilog_sources=verilog_sources,
        includes=includes,
        toplevel=toplevel,
        module=module,
        parameters=parameters,
        sim_build=sim_build,
        extra_env=extra_env,
        testcase='simple_test',
        force_compile=True,
        waves = os.environ.get('WAVES') == '1',
        defines = ['LUT_PATH=\"../../tests\"'],   # used by DDS core
        compile_args = compile_args
    )

@pytest.mark.parametrize('FILE', ['772850KHz_3840KSPS_low_gain'])
@pytest.mark.parametrize('HALF_CP_ADVANCE', [0, 1])
@pytest.mark.parametrize('MULT_REUSE', [4])
@pytest.mark.parametrize('RND_JITTER', [0])  # disable RND_JITTER for now
@pytest.mark.parametrize('HAS_CFO_COR', [0])
def test_NFFT8_3840KSPS_recording(FILE, HALF_CP_ADVANCE, MULT_REUSE, RND_JITTER, HAS_CFO_COR):
    test(IN_DW = 32, OUT_DW = 32, TAP_DW = 32, WINDOW_LEN = 8, CFO = 0, HALF_CP_ADVANCE = HALF_CP_ADVANCE, USE_TAP_FILE = 1, LLR_DW = 8,
        NFFT = 8, MULT_REUSE = MULT_REUSE, INITIAL_DETECTION_SHIFT = 3, INITIAL_CFO_MODE = 1, RND_JITTER = RND_JITTER,
        FILE = FILE, HAS_CFO_COR = HAS_CFO_COR)

@pytest.mark.parametrize('FILE', ['30720KSPS_dl_signal'])
@pytest.mark.parametrize('HALF_CP_ADVANCE', [1])
@pytest.mark.parametrize('MULT_REUSE', [4])
@pytest.mark.parametrize('RND_JITTER', [0])
@pytest.mark.parametrize('HAS_CFO_COR', [0])
def test_NFFT9_7680KSPS_ideal(FILE, HALF_CP_ADVANCE, MULT_REUSE, RND_JITTER, HAS_CFO_COR):
    test(IN_DW = 32, OUT_DW = 32, TAP_DW = 32, WINDOW_LEN = 8, CFO = 0, HALF_CP_ADVANCE = HALF_CP_ADVANCE, USE_TAP_FILE = 1, LLR_DW = 8,
        NFFT = 9, MULT_REUSE = MULT_REUSE, INITIAL_DETECTION_SHIFT = 3, INITIAL_CFO_MODE = 1, RND_JITTER = RND_JITTER,
        FILE = FILE, HAS_CFO_COR = HAS_CFO_COR)
    
@pytest.mark.parametrize('FILE', ['763450KHz_7680KSPS_low_gain'])
@pytest.mark.parametrize('HALF_CP_ADVANCE', [1])
@pytest.mark.parametrize('MULT_REUSE', [4])
@pytest.mark.parametrize('RND_JITTER', [0])
@pytest.mark.parametrize('HAS_CFO_COR', [0])
def test_NFFT9_7680KSPS_recording(FILE, HALF_CP_ADVANCE, MULT_REUSE, RND_JITTER, HAS_CFO_COR):
    test(IN_DW = 32, OUT_DW = 32, TAP_DW = 32, WINDOW_LEN = 8, CFO = 0, HALF_CP_ADVANCE = HALF_CP_ADVANCE, USE_TAP_FILE = 1, LLR_DW = 8,
        NFFT = 9, MULT_REUSE = MULT_REUSE, INITIAL_DETECTION_SHIFT = 3, INITIAL_CFO_MODE = 1, RND_JITTER = RND_JITTER, FILE = FILE,
        HAS_CFO_COR = HAS_CFO_COR)

if __name__ == '__main__':
    os.environ['SIM'] = 'verilator'
    os.environ['PLOTS'] = '1'
    os.environ['WAVES'] = '1'
    if True:
        test(IN_DW = 32, OUT_DW = 32, TAP_DW = 32, WINDOW_LEN = 8, CFO = 0, HALF_CP_ADVANCE = 1, USE_TAP_FILE = 1, LLR_DW = 8,
            NFFT = 9, MULT_REUSE = 0, INITIAL_DETECTION_SHIFT = 3, INITIAL_CFO_MODE = 1, RND_JITTER = 0,
            FILE = '763450KHz_7680KSPS_low_gain', HAS_CFO_COR = 0)
    else:
        test(IN_DW = 32, OUT_DW = 32, TAP_DW = 32, WINDOW_LEN = 8, CFO = 0, HALF_CP_ADVANCE = 0, USE_TAP_FILE = 1, LLR_DW = 8,
            NFFT = 8, MULT_REUSE = 1, INITIAL_DETECTION_SHIFT = 4, INITIAL_CFO_MODE = 1, RND_JITTER = 0)
