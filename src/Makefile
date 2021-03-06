#!gmake

TOOL=qtip

.PHONY: all
all: ../$(TOOL)-parse \
	   ../$(TOOL)-rewrite \
		 ../VERSION

.PHONY: allall
allall: all ../$(TOOL)-parse-debug \
            ../$(TOOL)-rewrite-debug \
						../$(TOOL)-predmerge-test \
						../$(TOOL)-fasta-test

PARSE_DEPS = $(TOOL)_parse.cpp simplesim.cpp input_model.cpp ranlib.cpp rnglib.cpp fasta.cpp

REWRITE_DEPS = $(TOOL)_rewrite.cpp predmerge.cpp

# git tag -a v1.4.1 -m 'Version 1.4.1'
# git push --tags
../VERSION:
	git describe --tags --long > $@

../$(TOOL)-parse: $(PARSE_DEPS)
	g++ -O3 $(EXTRA_FLAGS) -o $@ $^

# note, on some JHU systems I have to use -gdwarf-3
../$(TOOL)-parse-debug: $(PARSE_DEPS)
	g++ -g -O0 $(EXTRA_FLAGS) -o $@ $^

../$(TOOL)-rewrite: $(REWRITE_DEPS)
	g++ -O3 $(EXTRA_FLAGS) -o $@ $^

# note, on some JHU systems I have to use -gdwarf-3
../$(TOOL)-rewrite-debug: $(REWRITE_DEPS)
	g++ -g -O0 $(EXTRA_FLAGS) -o $@ $^

../$(TOOL)-predmerge-test: predmerge.cpp predmerge.h
	g++ -g -O0 -DPREDMERGE_MAIN -o $@ $<

../$(TOOL)-fasta-test: fasta.cpp fasta.h
	g++ -g -O0 -DFASTA_MAIN -o $@ $<

.PHONY: clean
clean:
	rm -rf ../*.dSYM
	rm -f ../$(TOOL)-parse* ../$(TOOL)-rewrite* ../$(TOOL)-*-test
	rm -f ../VERSION
	rm -f ../*.pyc
