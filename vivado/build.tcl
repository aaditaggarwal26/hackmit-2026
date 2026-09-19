# Orbit edge node — headless Vivado flow (in-memory project, no .xpr).
#   vivado -mode batch -source vivado/build.tcl            (from the repo root)
# Produces vivado/build/orbit.bit (+ .mcs for the QSPI flash) and the reports
# under vivado/reports/ that tools on main parse. Fails loudly on negative
# setup slack; nothing is written to the bitstream in that case.

set root      [file normalize [file join [file dirname [info script]] ..]]
set build_dir [file join $root vivado build]
set rep_dir   [file join $root vivado reports]
file mkdir $build_dir
file mkdir $rep_dir

set part xc7a100tcsg324-1
set top  top_edge_node

# every RTL file except the phase-0 probe; orbit_params.vh is found via include_dirs
set rtl_files {}
foreach f [lsort [glob [file join $root rtl *.v]]] {
    if {[file tail $f] ne "hello.v"} { lappend rtl_files $f }
}
read_verilog $rtl_files
read_xdc [file join $root vivado arty_a7_100t.xdc]

synth_design -top $top -part $part -include_dirs [file join $root rtl] -flatten_hierarchy rebuilt
report_utilization -hierarchical -file [file join $rep_dir utilization_synth.txt]

opt_design
place_design
phys_opt_design
route_design

report_utilization    -file [file join $rep_dir utilization.txt]
report_timing_summary -file [file join $rep_dir timing.txt] -delay_type min_max -report_unconstrained \
                      -max_paths 10 -input_pins
report_drc            -file [file join $rep_dir drc.txt]

# power: use a switching-activity file if a simulation produced one
set saif [file join $build_dir activity.saif]
if {[file exists $saif]} {
    read_saif -file $saif -strip_path ${top}
    puts "INFO: power estimate uses $saif"
} else {
    puts "INFO: no $saif — power report uses default (vectorless) activity; run a SAIF-producing sim for a real number"
}
report_power -file [file join $rep_dir power.txt]

# Fail loudly on timing so a broken build cannot be flashed by accident.
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts "INFO: worst setup slack = $wns ns, worst hold slack = $whs ns"
if {$wns < 0} { error "TIMING FAILED: worst negative setup slack $wns ns (see $rep_dir/timing.txt)" }
if {$whs < 0} { error "TIMING FAILED: worst negative hold slack $whs ns (see $rep_dir/timing.txt)" }

write_checkpoint -force [file join $build_dir orbit_routed.dcp]
write_bitstream  -force [file join $build_dir orbit.bit]
# QSPI image for the on-board 16 MB flash (SPIx4, matches BITSTREAM.CONFIG.SPI_BUSWIDTH 4 in the XDC)
write_cfgmem -force -format mcs -interface SPIx4 -size 16 \
             -loadbit "up 0x0 [file join $build_dir orbit.bit]" [file join $build_dir orbit.mcs]
puts "INFO: wrote [file join $build_dir orbit.bit] and orbit.mcs"
